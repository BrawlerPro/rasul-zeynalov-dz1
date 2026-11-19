import http from 'k6/http';
import exec from 'k6/execution';
import { Rate, Trend } from 'k6/metrics';

const mode = __ENV.MODE || 'mixed';
const rate = Number(__ENV.RATE || 2);
const baseURL = __ENV.BASE_URL || 'http://127.0.0.1:20812';
const account = __ENV.SEED_LOGIN || 'rasul';

const latency = new Trend('dz2_duration', true);
const errors = new Rate('dz2_errors');
const server = new Trend('dz2_app_ms', true);
const db = new Trend('dz2_db_ms', true);
const queries = new Trend('dz2_db_queries');

const operations = [
  { name: 'list', weight: 21, path: '/api/tickets?page=1&size=20' },
  { name: 'filtered_list', weight: 17, path: '/api/tickets?page=1&size=20&status=new' },
  { name: 'detail', weight: 25 },
  { name: 'categories', weight: 6, path: '/api/categories' },
  { name: 'create', weight: 6 },
  { name: 'comment', weight: 13 },
  { name: 'status', weight: 10 },
  { name: 'summary', weight: 2, path: '/api/summary' },
];

if (!['mixed', 'heavy'].includes(mode)) {
  throw new Error('MODE: mixed или heavy');
}
if (!Number.isSafeInteger(rate) || rate < 1) {
  throw new Error('RATE должен быть положительным целым числом');
}

const scenario = {
  executor: 'constant-arrival-rate',
  rate,
  timeUnit: '1s',
  duration: __ENV.DURATION || '30s',
  preAllocatedVUs: Number(__ENV.VUS || 200),
  maxVUs: Number(__ENV.VUS || 200),
  gracefulStop: '15s',
};
const opNames = mode === 'heavy' ? ['summary'] : operations.map((op) => op.name);
const taggedMetrics = {};
for (const op of opNames) {
  for (const metric of ['dz2_duration', 'dz2_errors', 'dz2_app_ms', 'dz2_db_ms', 'dz2_db_queries']) {
    taggedMetrics[`${metric}{op:${op}}`] = [];
  }
}

export const options = {
  scenarios: mode === 'mixed' ? { mixed: scenario } : { heavy: scenario },
  thresholds: { dz2_duration: ['p(95)<500'], dz2_errors: ['rate<0.01'], ...taggedMetrics },
  summaryTrendStats: ['avg', 'med', 'p(95)', 'max', 'count'],
  discardResponseBodies: true,
};

export function setup() {
  if (!__ENV.SEED_PASSWORD) {
    throw new Error('Задайте SEED_PASSWORD для входа на учебный стенд');
  }
  const response = http.post(`${baseURL}/api/login`, JSON.stringify({
    login: account,
    password: __ENV.SEED_PASSWORD,
  }), {
    headers: { 'Content-Type': 'application/json' },
    responseType: 'text',
    tags: { name: 'login' },
  });
  const token = response.status === 200 ? response.json('token') : null;
  if (!token || response.json('user.role') !== 'staff') {
    throw new Error(`Ожидался вход сотрудника, HTTP ${response.status}`);
  }
  return { token };
}

function pickOperation(iteration) {
  if (mode === 'heavy') return operations[operations.length - 1];
  let slot = (iteration * 37) % 100;
  for (const operation of operations) {
    if (slot < operation.weight) return operation;
    slot -= operation.weight;
  }
  throw new Error('Сумма весов должна составлять 100');
}

function requestSpec(operation, iteration) {
  const id = 1 + (iteration * 73 + 113) % 50000;
  switch (operation.name) {
    case 'detail':
      return { method: 'GET', path: `/api/tickets/${id}`, status: 200 };
    case 'create':
      return {
        method: 'POST', path: '/api/tickets', status: 201,
        body: {
          title: `rasul_zeynalov ДЗ 2 / заявка ${iteration}`,
          body: 'Проверка регистрации обращения под нагрузкой',
          category_id: 1 + iteration % 10,
        },
      };
    case 'comment': {
      const openID = id % 3 === 0 ? id - 1 : id;
      return {
        method: 'POST', path: `/api/tickets/${openID}/comments`, status: 201,
        body: { body: 'Ответ сотрудника в нагрузочном испытании' },
        conflict: 'Переписка закрытой заявки запрещена',
      };
    }
    case 'status':
      return {
        method: 'PATCH', path: `/api/tickets/${id}/status`, status: 200,
        body: { status: id % 3 === 2 ? 'closed' : 'in_progress' },
        conflict: 'Недопустимый переход статуса',
      };
    default:
      return { method: 'GET', path: operation.path, status: 200 };
  }
}

export default function (data) {
  const iteration = exec.scenario.iterationInTest;
  const operation = pickOperation(iteration);
  const spec = requestSpec(operation, iteration);
  const tags = { name: operation.name, op: operation.name };
  const response = http.request(spec.method, baseURL + spec.path,
    spec.body ? JSON.stringify(spec.body) : null, {
      headers: { Authorization: `Bearer ${data.token}`, 'Content-Type': 'application/json' },
      tags,
      timeout: '12s',
      responseType: spec.conflict ? 'text' : 'none',
      responseCallback: spec.conflict
        ? http.expectedStatuses(spec.status, 409)
        : http.expectedStatuses(spec.status),
    });

  let expectedConflict = false;
  if (response.status === 409 && spec.conflict) {
    try {
      expectedConflict = response.json('detail') === spec.conflict;
    } catch (_) {
      expectedConflict = false;
    }
  }
  const failed = response.status !== spec.status && !expectedConflict;
  latency.add(response.timings.duration, tags);
  errors.add(failed, tags);
  for (const [header, metric] of [
    ['X-App-Ms', server], ['X-Db-Ms', db], ['X-Db-Queries', queries],
  ]) {
    if (response.headers[header] !== undefined) {
      metric.add(Number(response.headers[header]), tags);
    }
  }
  if (failed) {
    console.warn(JSON.stringify({
      utc: new Date().toISOString(), operation: operation.name,
      method: spec.method, path: spec.path, status: response.status,
      error_code: response.error_code,
    }));
  }
}

export function handleSummary(data) {
  return { [__ENV.SUMMARY_PATH || 'dz2-summary.json']: JSON.stringify(data, null, 2) };
}
