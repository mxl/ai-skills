#!/usr/bin/env node

import https from 'node:https';
import net from 'node:net';
import dns from 'node:dns/promises';
import { domainToASCII } from 'node:url';

const IANA_RDAP_BOOTSTRAP = 'https://data.iana.org/rdap/dns.json';
const TCI_WHOIS_SERVER = 'whois.tcinet.ru';
const IANA_WHOIS_SERVER = 'whois.iana.org';

const VALID_METHODS = new Set(['auto', 'rdap', 'whois', 'dns']);

function usage() {
  return `Usage: node domain-check/scripts/domain-check.mjs [options] <domain...>

Check exact domain availability using RDAP, WHOIS, and DNS diagnostics.

Options:
  --method <method>    Check method: auto, rdap, whois, dns (default: auto)
  --delay-ms <ms>      Delay between requests in milliseconds (default: 0)
  --timeout-ms <ms>    Network timeout in milliseconds (default: 8000)
  --include-dns        Include DNS diagnostics (NS/SOA) in the results
  --json               Print machine-readable JSON only
  --help               Show this help
`;
}

function parseArgs(argv) {
  const options = {
    method: 'auto',
    delayMs: 0,
    timeoutMs: 8000,
    includeDns: false,
    json: false,
    domains: [],
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else if (arg === '--json') {
      options.json = true;
    } else if (arg === '--include-dns') {
      options.includeDns = true;
    } else if (arg === '--method') {
      const next = argv[i + 1];
      if (!next) throw new Error('--method requires a value');
      options.method = next.toLowerCase();
      i += 1;
    } else if (arg === '--delay-ms') {
      const next = argv[i + 1];
      if (!next) throw new Error('--delay-ms requires a value');
      options.delayMs = parseInt(next, 10);
      if (isNaN(options.delayMs)) throw new Error('--delay-ms must be a number');
      i += 1;
    } else if (arg === '--timeout-ms') {
      const next = argv[i + 1];
      if (!next) throw new Error('--timeout-ms requires a value');
      options.timeoutMs = parseInt(next, 10);
      if (isNaN(options.timeoutMs)) throw new Error('--timeout-ms must be a number');
      i += 1;
    } else if (arg.startsWith('-')) {
      throw new Error(`Unknown option: ${arg}`);
    } else {
      options.domains.push(arg);
    }
  }

  if (!VALID_METHODS.has(options.method)) {
    throw new Error(`Unsupported method: ${options.method}. Use auto, rdap, whois, or dns.`);
  }

  return options;
}

function normalizeDomain(raw) {
  const domain = raw.trim().toLowerCase().replace(/\.$/, '');
  if (!domain) return null;
  if (domain.includes('://') || domain.includes('/') || domain.includes('?') || domain.includes('#')) {
    throw new Error(`Invalid domain: ${raw}. URLs, paths, and queries are not allowed.`);
  }
  if (/\s/.test(domain)) {
    throw new Error(`Invalid domain: ${raw}. Spaces are not allowed.`);
  }
  const asciiDomain = domainToASCII(domain);
  return {
    input: raw,
    unicode: domain,
    ascii: asciiDomain,
  };
}

async function fetchRDAPBootstrap() {
  return new Promise((resolve, reject) => {
    https.get(IANA_RDAP_BOOTSTRAP, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch (e) {
          reject(new Error(`Failed to parse RDAP bootstrap: ${e.message}`));
        }
      });
    }).on('error', reject);
  });
}

async function queryRDAP(domain, bootstrap, timeoutMs) {
  const tld = domain.split('.').pop().toLowerCase();
  const service = bootstrap.services.find(s => s[0].includes(tld));
  const server = service ? service[1][0] : null;
  if (!server) {
    throw new Error(`No RDAP server found for TLD .${tld}`);
  }

  const url = `${server}/domain/${domain}`;
  
  return new Promise((resolve, reject) => {
    const req = https.request(url, { timeout: timeoutMs }, (res) => {
      if (res.statusCode === 200) {
        let data = '';
        res.on('data', (chunk) => data += chunk);
        res.on('end', () => resolve({ status: 'registered', source: 'RDAP', raw: data }));
      } else if (res.statusCode === 404) {
        resolve({ status: 'appears_unregistered', source: 'RDAP', raw: null });
      } else {
        reject(new Error(`RDAP returned HTTP ${res.statusCode}`));
      }
    });
    req.on('error', reject);
    req.end();
  });
}

async function queryWhois(domain, timeoutMs) {
  let server = null;
  const tld = domain.split('.').pop().toLowerCase();
  
  if (tld === 'ru' || tld === 'xn--p1ai') {
    server = TCI_WHOIS_SERVER;
  } else {
    try {
      server = await discoverWhoisServer(tld, timeoutMs);
    } catch (e) {
      throw new Error(`Could not discover WHOIS server for .${tld}: ${e.message}`);
    }
  }

  return new Promise((resolve, reject) => {
    const client = net.createConnection({ host: server, port: 43, timeout: timeoutMs }, () => {
      client.write(`${domain}\r\n`);
    });

    let data = '';
    client.on('data', (chunk) => {
      data += chunk.toString('utf8');
      // Many WHOIS servers close connection after sending response.
      // Some might stay open, so we use a simple heuristic or wait for close.
    });

    client.on('end', () => {
      const normalized = data.toLowerCase();
      if (normalized.includes('no entries found') || 
          normalized.includes('no match') || 
          normalized.includes('not found') || 
          normalized.includes('no data found')) {
        resolve({ status: 'appears_unregistered', source: `WHOIS (${server})`, raw: data });
      } else if (normalized.includes('domain:') || 
                 normalized.includes('registrar:') || 
                 normalized.includes('state: registered')) {
        resolve({ status: 'registered', source: `WHOIS (${server})`, raw: data });
      } else {
        resolve({ status: 'unknown', source: `WHOIS (${server})`, raw: data });
      }
    });

    client.on('error', (err) => reject(err));
    client.on('timeout', () => {
      client.destroy();
      reject(new Error('WHOIS request timed out'));
    });
  });
}

async function discoverWhoisServer(tld, timeoutMs) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection({ host: IANA_WHOIS_SERVER, port: 43, timeout: timeoutMs }, () => {
      client.write(`${tld}\r\n`);
    });

    let data = '';
    client.on('data', (chunk) => {
      data += chunk.toString('utf8');
    });

    client.on('end', () => {
      const match = data.match(/whois:\s*(\S+)/i);
      if (match && match[1]) {
        resolve(match[1].toLowerCase());
      } else {
        reject(new Error('IANA WHOIS server did not provide a referral for this TLD'));
      }
    });

    client.on('error', reject);
    client.on('timeout', () => {
      client.destroy();
      reject(new Error('IANA WHOIS discovery timed out'));
    });
  });
}

async function queryDns(domain, timeoutMs) {
  try {
    // We check NS and SOA records.
    const ns = await dns.resolveNs(domain).catch(() => []);
    const soa = await dns.resolve(domain, 'SOA').catch(() => []);
    
    if (ns.length > 0 || soa.length > 0) {
      return { status: 'registered', source: 'DNS', detail: `Found ${ns.length} NS and ${soa.length} SOA records` };
    }
    return { status: 'unknown', source: 'DNS', detail: 'No NS or SOA records found (could be unregistered or non-delegated)' };
  } catch (error) {
    return { status: 'provider_error', source: 'DNS', message: error.message };
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(usage());
    return;
  }

  if (options.domains.length === 0) {
    console.error('Error: Provide at least one exact domain name to check');
    process.exit(1);
  }

  const normalized = [];
  const seen = new Set();
  for (const raw of options.domains) {
    try {
      const norm = normalizeDomain(raw);
      if (norm && !seen.has(norm.ascii)) {
        normalized.push(norm);
        seen.add(norm.ascii);
      }
    } catch (e) {
      console.error(`Error: ${e.message}`);
      process.exit(1);
    }
  }

  let bootstrap = null;
  if (options.method === 'auto' || options.method === 'rdap') {
    try {
      bootstrap = await fetchRDAPBootstrap();
    } catch (e) {
      console.error(`Warning: Could not fetch RDAP bootstrap: ${e.message}. Falling back to WHOIS.`);
    }
  }

  const finalResults = [];
  for (let i = 0; i < normalized.length; i++) {
    const domainObj = normalized[i];
    const domain = domainObj.ascii;
    const tld = domain.split('.').pop().toLowerCase();
    
    let result = null;
    let methodUsed = options.method;

    try {
      if (options.method === 'dns') {
        result = await queryDns(domain, options.timeoutMs);
      } else if (options.method === 'rdap') {
        if (!bootstrap) throw new Error('RDAP bootstrap not available');
        result = await queryRDAP(domain, bootstrap, options.timeoutMs);
      } else if (options.method === 'whois') {
        result = await queryWhois(domain, options.timeoutMs);
      } else {
        // auto method
        if ((tld === 'ru' || tld === 'xn--p1ai')) {
          result = await queryWhois(domain, options.timeoutMs);
          methodUsed = 'whois';
        } else if (bootstrap) {
          try {
            result = await queryRDAP(domain, bootstrap, options.timeoutMs);
            methodUsed = 'rdap';
          } catch (e) {
            result = await queryWhois(domain, options.timeoutMs);
            methodUsed = 'whois';
          }
        } else {
          result = await queryWhois(domain, options.timeoutMs);
          methodUsed = 'whois';
        }
      }
    } catch (error) {
      result = { status: 'provider_error', source: 'system', message: error.message };
    }

    const dnsDiagnostics = options.includeDns ? await queryDns(domain, options.timeoutMs) : null;

    finalResults.push({
      domain: domainObj.unicode,
      asciiDomain: domainObj.ascii,
      tld,
      method: methodUsed,
      status: result.status,
      source: result.source,
      message: result.message || null,
      dns: dnsDiagnostics,
    });

    if (i < normalized.length - 1 && options.delayMs > 0) {
      await new Promise(resolve => setTimeout(resolve, options.delayMs));
    }
  }

  const output = {
    method: options.method,
    checkedAt: new Date().toISOString(),
    results: finalResults,
  };

  if (options.json) {
    console.log(JSON.stringify(output, null, 2));
  } else {
    const available = finalResults.filter(r => r.status === 'appears_unregistered');
    const registered = finalResults.filter(r => r.status === 'registered');
    const others = finalResults.filter(r => r.status !== 'appears_unregistered' && r.status !== 'registered');

    if (available.length > 0) {
      console.log('Appears unregistered (no registry record found):');
      for (const r of available) {
        console.log(`  ${r.domain} (${r.source})`);
      }
      console.log('');
    }

    if (registered.length > 0) {
      console.log('Registered:');
      for (const r of registered) {
        console.log(`  ${r.domain} (${r.source})`);
      }
      console.log('');
    }

    if (others.length > 0) {
      console.log('Unknown or errored:');
      for (const r of others) {
        console.log(`  ${r.domain}: ${r.message || r.status}`);
      }
    }
    
    if (available.length === 0 && registered.length === 0 && others.length === 0) {
      console.log('No results found.');
    }
  }
}

main().catch((error) => {
  console.error(`Error: ${error.message}`);
  process.exitCode = 1;
});
