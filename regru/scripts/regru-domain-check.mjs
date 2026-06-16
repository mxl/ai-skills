#!/usr/bin/env node

import fs from 'node:fs';
import https from 'node:https';
import { URLSearchParams } from 'node:url';

const API_URL = 'https://api.reg.ru/api/regru2/domain/check';
const VALID_CURRENCIES = new Set(['RUR', 'UAH', 'USD', 'EUR']);

function usage() {
  return `Usage: node regru/scripts/regru-domain-check.mjs [options] <domain...>

Check exact domain availability through REG.RU API 2 domain/check.

Required environment:
  REGRU_USERNAME       REG.RU username/login
  REGRU_PASSWORD       REG.RU password or alternative API password

Optional environment:
  REGRU_SSL_CERT_PATH  Client SSL certificate path
  REGRU_SSL_KEY_PATH   Client SSL private key path

Options:
  --currency <code>    RUR, UAH, USD, or EUR (default: RUR)
  --premium-as-taken   Ask REG.RU to report premium domains as taken
  --json               Print machine-readable JSON only
  --help               Show this help
`;
}

function parseArgs(argv) {
  const options = {
    currency: 'RUR',
    premiumAsTaken: false,
    json: false,
    domains: [],
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else if (arg === '--json') {
      options.json = true;
    } else if (arg === '--premium-as-taken') {
      options.premiumAsTaken = true;
    } else if (arg === '--currency') {
      const next = argv[i + 1];
      if (!next) {
        throw new Error('--currency requires a value');
      }
      options.currency = next.toUpperCase();
      i += 1;
    } else if (arg.startsWith('--currency=')) {
      options.currency = arg.slice('--currency='.length).toUpperCase();
    } else if (arg.startsWith('-')) {
      throw new Error(`Unknown option: ${arg}`);
    } else {
      options.domains.push(arg);
    }
  }

  if (!VALID_CURRENCIES.has(options.currency)) {
    throw new Error(`Unsupported currency: ${options.currency}. Use RUR, UAH, USD, or EUR.`);
  }

  return options;
}

function normalizeDomains(domains) {
  const seen = new Set();
  const result = [];

  for (const raw of domains) {
    const domain = raw.trim().replace(/^https?:\/\//i, '').replace(/\/.*$/, '').toLowerCase();
    if (!domain || seen.has(domain)) {
      continue;
    }
    seen.add(domain);
    result.push(domain);
  }

  return result;
}

function readCredentials(env) {
  const username = env.REGRU_USERNAME;
  const password = env.REGRU_PASSWORD;
  const certPath = env.REGRU_SSL_CERT_PATH;
  const keyPath = env.REGRU_SSL_KEY_PATH;

  if (!username || !password) {
    throw new Error('Missing required environment variables: REGRU_USERNAME and REGRU_PASSWORD');
  }
  if ((certPath && !keyPath) || (!certPath && keyPath)) {
    throw new Error('Set both REGRU_SSL_CERT_PATH and REGRU_SSL_KEY_PATH, or neither');
  }

  const tls = {};
  if (certPath && keyPath) {
    tls.cert = fs.readFileSync(certPath);
    tls.key = fs.readFileSync(keyPath);
  }

  return { username, password, tls, sslEnabled: Boolean(certPath && keyPath) };
}

function postForm(url, params, tlsOptions) {
  const body = params.toString();
  const target = new URL(url);

  const requestOptions = {
    method: 'POST',
    hostname: target.hostname,
    path: `${target.pathname}${target.search}`,
    port: target.port || 443,
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'Content-Length': Buffer.byteLength(body),
    },
    ...tlsOptions,
  };

  return new Promise((resolve, reject) => {
    const req = https.request(requestOptions, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        if (res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`HTTP ${res.statusCode}: ${text.slice(0, 500)}`));
          return;
        }
        resolve(text);
      });
    });

    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function checkDomains(domains, options, credentials) {
  const input = {
    username: credentials.username,
    password: credentials.password,
    domains: domains.map((dname) => ({ dname })),
    currency: options.currency,
  };

  if (options.premiumAsTaken) {
    input.premium_as_taken = 1;
  }

  const params = new URLSearchParams();
  params.append('output_content_type', 'json');
  params.append('input_format', 'json');
  params.append('input_data', JSON.stringify(input));

  const responseText = await postForm(API_URL, params, credentials.tls);
  let response;
  try {
    response = JSON.parse(responseText);
  } catch (error) {
    throw new Error(`REG.RU returned non-JSON response: ${responseText.slice(0, 500)}`);
  }

  if (response.result !== 'success') {
    const code = response.error_code ? ` ${response.error_code}` : '';
    const text = response.error_text ? `: ${response.error_text}` : '';
    if (response.error_code === 'RESELLER_AUTH_FAILED') {
      throw new Error(`REG.RU API error${code}${text}. The domain/check endpoint is partner/reseller-only; enable partner API access for this account or use reseller credentials.`);
    }
    throw new Error(`REG.RU API error${code}${text}`);
  }

  const items = response.answer?.domains;
  if (!Array.isArray(items)) {
    throw new Error('REG.RU response did not include answer.domains array');
  }

  return {
    result: 'success',
    endpoint: API_URL,
    ssl_enabled: credentials.sslEnabled,
    domains: items,
  };
}

function classify(item) {
  return item.result === 'Available' ? 'available' : 'unavailable';
}

function formatLine(item) {
  const parts = [item.dname || item.domain_name || '(unknown)', '-', item.result || 'Unknown'];
  if (item.error_code) {
    parts.push(`(${item.error_code})`);
  }
  if (item.is_premium) {
    parts.push('[premium]');
  }
  if (item.price) {
    parts.push(`price: ${item.price}${item.currency ? ` ${item.currency}` : ''}`);
  }
  if (item.renew_price) {
    parts.push(`renew: ${item.renew_price}${item.currency ? ` ${item.currency}` : ''}`);
  }
  return parts.join(' ');
}

function printHuman(output) {
  const available = output.domains.filter((item) => classify(item) === 'available');
  const unavailable = output.domains.filter((item) => classify(item) !== 'available');

  console.log('Available domains:');
  if (available.length === 0) {
    console.log('  None');
  } else {
    for (const item of available) {
      console.log(`  ${formatLine(item)}`);
    }
  }

  console.log('');
  console.log('Unavailable, invalid, or errored domains:');
  if (unavailable.length === 0) {
    console.log('  None');
  } else {
    for (const item of unavailable) {
      console.log(`  ${formatLine(item)}`);
    }
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(usage());
    return;
  }

  const domains = normalizeDomains(options.domains);
  if (domains.length === 0) {
    throw new Error('Provide at least one exact domain name to check');
  }

  const credentials = readCredentials(process.env);
  const output = await checkDomains(domains, options, credentials);

  if (options.json) {
    console.log(JSON.stringify(output, null, 2));
  } else {
    printHuman(output);
  }
}

main().catch((error) => {
  console.error(`Error: ${error.message}`);
  process.exitCode = 1;
});
