#!/usr/bin/env node

import https from 'node:https';
import net from 'node:net';
import dns from 'node:dns/promises';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { domainToASCII } from 'node:url';

const IANA_RDAP_BOOTSTRAP = 'https://data.iana.org/rdap/dns.json';
const TCI_WHOIS_SERVER = 'whois.tcinet.ru';
const IANA_WHOIS_SERVER = 'whois.iana.org';
const BOOTSTRAP_CACHE_FILE = path.join(os.tmpdir(), 'domain-check-rdap-bootstrap.json');
const BOOTSTRAP_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h

const VALID_METHODS = new Set(['auto', 'rdap', 'whois', 'dns']);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Retry helper with exponential backoff + jitter for transient errors.
async function withRetry(fn, { retries = 2, baseDelayMs = 500 } = {}) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (!err || !err.retryable || attempt === retries) throw err;
      const delay = baseDelayMs * 2 ** attempt + Math.floor(Math.random() * 200);
      await sleep(delay);
    }
  }
  throw lastErr;
}

function makeRetryable(err) {
  if (err) err.retryable = true;
  return err;
}

function usage() {
  return `Usage: node domain-check/scripts/domain-check.mjs [options] <domain...>

Check exact domain availability using RDAP, WHOIS, and DNS diagnostics.

Options:
  --method <method>    Check method: auto, rdap, whois, dns (default: auto)
  --concurrency <n>    Parallel checks (default: 5)
  --delay-ms <ms>      Delay after each request per worker in ms (default: 0)
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
    concurrency: 5,
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
    } else if (arg === '--concurrency') {
      const next = argv[i + 1];
      if (!next) throw new Error('--concurrency requires a value');
      options.concurrency = parseInt(next, 10);
      if (isNaN(options.concurrency) || options.concurrency < 1) {
        throw new Error('--concurrency must be a positive number');
      }
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

function readBootstrapCache() {
  try {
    const stat = fs.statSync(BOOTSTRAP_CACHE_FILE);
    if (Date.now() - stat.mtimeMs > BOOTSTRAP_CACHE_TTL_MS) return null;
    return JSON.parse(fs.readFileSync(BOOTSTRAP_CACHE_FILE, 'utf8'));
  } catch {
    return null;
  }
}

function writeBootstrapCache(data) {
  try {
    fs.writeFileSync(BOOTSTRAP_CACHE_FILE, JSON.stringify(data), 'utf8');
  } catch {
    // Cache write is best-effort; ignore failures.
  }
}

function downloadBootstrap(timeoutMs) {
  return new Promise((resolve, reject) => {
    const req = https.get(IANA_RDAP_BOOTSTRAP, { timeout: timeoutMs }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        reject(makeRetryable(new Error(`RDAP bootstrap returned HTTP ${res.statusCode}`)));
        return;
      }
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch (e) {
          reject(new Error(`Failed to parse RDAP bootstrap: ${e.message}`));
        }
      });
    });
    req.on('error', (err) => reject(makeRetryable(err)));
    req.on('timeout', () => {
      req.destroy();
      reject(makeRetryable(new Error('RDAP bootstrap request timed out')));
    });
  });
}

async function fetchRDAPBootstrap(timeoutMs) {
  const cached = readBootstrapCache();
  if (cached) return cached;
  const data = await withRetry(() => downloadBootstrap(timeoutMs), { retries: 2, baseDelayMs: 400 });
  writeBootstrapCache(data);
  return data;
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
        res.resume();
        resolve({ status: 'appears_unregistered', source: 'RDAP', raw: null });
      } else {
        res.resume();
        const err = new Error(`RDAP returned HTTP ${res.statusCode}`);
        if (res.statusCode === 429 || res.statusCode >= 500) makeRetryable(err);
        reject(err);
      }
    });
    req.on('error', (err) => reject(makeRetryable(err)));
    req.on('timeout', () => {
      req.destroy();
      reject(makeRetryable(new Error('RDAP request timed out')));
    });
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

    client.on('error', (err) => reject(makeRetryable(err)));
    client.on('timeout', () => {
      client.destroy();
      reject(makeRetryable(new Error('WHOIS request timed out')));
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

  // RDAP bootstrap is only needed when at least one non-.ru/.рф domain
  // goes through RDAP. Skip the network call entirely otherwise.
  const needsBootstrap =
    (options.method === 'auto' || options.method === 'rdap') &&
    normalized.some((d) => {
      const tld = d.ascii.split('.').pop().toLowerCase();
      return tld !== 'ru' && tld !== 'xn--p1ai';
    });

  let bootstrap = null;
  if (needsBootstrap) {
    try {
      bootstrap = await fetchRDAPBootstrap(options.timeoutMs);
    } catch (e) {
      console.error(`Warning: Could not fetch RDAP bootstrap: ${e.message}. Falling back to WHOIS.`);
    }
  }

  async function checkOne(domainObj) {
    const domain = domainObj.ascii;
    const tld = domain.split('.').pop().toLowerCase();
    let result = null;
    let methodUsed = options.method;

    try {
      if (options.method === 'dns') {
        result = await queryDns(domain, options.timeoutMs);
      } else if (options.method === 'rdap') {
        if (!bootstrap) throw new Error('RDAP bootstrap not available');
        result = await withRetry(() => queryRDAP(domain, bootstrap, options.timeoutMs));
      } else if (options.method === 'whois') {
        result = await withRetry(() => queryWhois(domain, options.timeoutMs));
      } else {
        // auto method
        if (tld === 'ru' || tld === 'xn--p1ai') {
          result = await withRetry(() => queryWhois(domain, options.timeoutMs));
          methodUsed = 'whois';
        } else if (bootstrap) {
          try {
            result = await withRetry(() => queryRDAP(domain, bootstrap, options.timeoutMs));
            methodUsed = 'rdap';
          } catch (e) {
            result = await withRetry(() => queryWhois(domain, options.timeoutMs));
            methodUsed = 'whois';
          }
        } else {
          result = await withRetry(() => queryWhois(domain, options.timeoutMs));
          methodUsed = 'whois';
        }
      }
    } catch (error) {
      result = { status: 'provider_error', source: 'system', message: error.message };
    }

    const dnsDiagnostics = options.includeDns ? await queryDns(domain, options.timeoutMs) : null;

    return {
      domain: domainObj.unicode,
      asciiDomain: domainObj.ascii,
      tld,
      method: methodUsed,
      status: result.status,
      source: result.source,
      message: result.message || null,
      dns: dnsDiagnostics,
    };
  }

  function streamLine(r) {
    if (options.json) return;
    let tag;
    if (r.status === 'appears_unregistered') tag = 'free ';
    else if (r.status === 'registered') tag = 'taken';
    else tag = 'err  ';
    const detail = r.status === 'appears_unregistered' || r.status === 'registered'
      ? `(${r.source})`
      : `: ${r.message || r.status}`;
    console.log(`[${tag}] ${r.domain} ${detail}`);
  }

  // Concurrency pool: stream each result as it completes so partial
  // progress survives an early termination.
  const finalResults = new Array(normalized.length);
  let nextIndex = 0;
  const workerCount = Math.min(options.concurrency, normalized.length);
  const workers = [];
  for (let w = 0; w < workerCount; w += 1) {
    workers.push((async () => {
      while (true) {
        const i = nextIndex;
        nextIndex += 1;
        if (i >= normalized.length) break;
        const res = await checkOne(normalized[i]);
        finalResults[i] = res;
        streamLine(res);
        if (options.delayMs > 0) await sleep(options.delayMs);
      }
    })());
  }
  await Promise.all(workers);

  if (options.json) {
    const output = {
      method: options.method,
      checkedAt: new Date().toISOString(),
      results: finalResults,
    };
    console.log(JSON.stringify(output, null, 2));
    return;
  }

  const available = finalResults.filter(r => r.status === 'appears_unregistered');
  const registered = finalResults.filter(r => r.status === 'registered');
  const others = finalResults.filter(r => r.status !== 'appears_unregistered' && r.status !== 'registered');

  console.log('');
  if (available.length > 0) {
    console.log('Appears unregistered (no registry record found):');
    for (const r of available) console.log(`  ${r.domain} (${r.source})`);
    console.log('');
  }
  if (registered.length > 0) {
    console.log('Registered:');
    for (const r of registered) console.log(`  ${r.domain} (${r.source})`);
    console.log('');
  }
  if (others.length > 0) {
    console.log('Unknown or errored:');
    for (const r of others) console.log(`  ${r.domain}: ${r.message || r.status}`);
    console.log('');
  }
  console.log(`Summary: ${available.length} free, ${registered.length} taken, ${others.length} errored (of ${finalResults.length}).`);
}

main().catch((error) => {
  console.error(`Error: ${error.message}`);
  process.exitCode = 1;
});
