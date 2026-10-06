// browse -> cart -> checkout synthetic transaction against Online Boutique's
// frontend service.
//
// Each iteration = one transaction = one W3C trace. A single trace_id is
// generated per iteration and reused across all HTTP requests in that
// transaction; each individual request gets its OWN new parent span_id, so
// the backend renders one trace per transaction while still letting each
// stage's span be told apart (per user requirement).
//
// Every request produces one JSONL record on stdout (prefixed "TXN_RECORD:"
// so the wrapper can separate it from k6's own log lines). Nothing is
// aggregated here -- aggregation/rollup happens downstream in
// recorder/transaction-recorder.py, working from these per-request records,
// never from the k6 end-of-test summary alone.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const RUN_ID = __ENV.RUN_ID || "run-unset";
const PROFILE_NAME = __ENV.PROFILE_NAME || "unset";
const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const TARGET_RPS = Number(__ENV.TARGET_RPS || 1);
const DURATION = __ENV.DURATION || "30s";
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 10);
const MAX_VUS = Number(__ENV.MAX_VUS || 50);
const STAGE_TIMEOUT = __ENV.STAGE_TIMEOUT || "5s";

const transactionsTotal = new Counter("sentrix_transactions_total");
const transactionsFailed = new Counter("sentrix_transactions_failed");

// k6's constant-arrival-rate `rate` must be an integer. TARGET_RPS is the
// profile's nominal requests-per-second and may be fractional (e.g. the
// "low" profile's 0.5). Scale up to whole-number-per-minute in that case
// instead of losing precision by rounding to the nearest integer rps.
let rate;
let timeUnit;
if (Number.isInteger(TARGET_RPS)) {
  rate = TARGET_RPS;
  timeUnit = "1s";
} else {
  rate = Math.round(TARGET_RPS * 60);
  timeUnit = "1m";
}

export const options = {
  scenarios: {
    browse_cart_checkout: {
      executor: "constant-arrival-rate",
      rate: rate,
      timeUnit: timeUnit,
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  // k6 metrics summary (iterations, dropped_iterations, http_req_duration
  // etc.) is exported separately via `k6 run --out json=...`; see
  // recorder/transaction-recorder.py for how the 1s rollup is built from it.
};

const PRODUCT_IDS = [
  "OLJCESPC7Z",
  "66VCHSJNUP",
  "1YMWWN1N4O",
  "L9ECAV7KIM",
  "2ZYFJ3GM2N",
  "0PUK6V6EV0",
  "LS4PSXUNUM",
  "9SIQT8TOJO",
  "6E92ZMYYFZ",
];

function randomHex(numBytes) {
  let s = "";
  for (let i = 0; i < numBytes; i++) {
    s += Math.floor(Math.random() * 256)
      .toString(16)
      .padStart(2, "0");
  }
  return s;
}

// One trace_id per transaction (iteration).
function newTraceId() {
  return randomHex(16); // 32 hex chars
}

// A NEW span_id (used as the traceparent's parent-id) per request/stage.
function newSpanId() {
  return randomHex(8); // 16 hex chars
}

function traceparent(traceId, spanId) {
  return `00-${traceId}-${spanId}-01`;
}

function nowIso() {
  return new Date().toISOString();
}

function emitRecord(rec) {
  // eslint-disable-next-line no-console
  console.log("TXN_RECORD:" + JSON.stringify(rec));
}

// Business-content assertions per stage, beyond plain HTTP status. Each
// checks a substring expected in a successful response body for that
// stage, based on Online Boutique's actual frontend templates.
const STAGE_BODY_ASSERTIONS = {
  browse_home: "Hot Products",
  browse_product: "Add To Cart",
  cart_add: null, // redirects to /cart; body assertion happens on cart_view instead
  cart_view: "Cart (",
  checkout: "Your order is complete!",
};

// k6's default `throw: false` means a network-level failure (including a
// request timeout) does NOT raise a JS exception -- http.get/post returns
// normally with res.status === 0 and an error_code describing what
// happened. A try/catch around the request call alone never sees these;
// it only catches the rarer case of k6 itself throwing (e.g. a malformed
// URL). Timeout must be read from res.error_code.
// Ref: https://grafana.com/docs/k6/latest/javascript-api/error-codes/
// Codes 1050-1059 are k6's "request timeout" family (generic timeout,
// connect/TLS/response timeouts). Matched by prefix rather than an exact
// list so we don't silently stop detecting timeouts if k6 adds a new
// sub-code in that family.
function isTimeoutError(res) {
  if (!res) return false; // exception path handles this case separately
  if (res.status !== 0) return false;
  if (res.error_code && res.error_code >= 1050 && res.error_code < 1060) {
    return true;
  }
  const err = (res.error || "").toLowerCase();
  return err.indexOf("timeout") !== -1 || err.indexOf("deadline exceeded") !== -1;
}

function doStage(ctx, stageName, requestFn) {
  const spanId = newSpanId();
  const tp = traceparent(ctx.traceId, spanId);
  const eventTimeStart = nowIso();
  const t0 = Date.now();

  let res;
  let exceptionMessage = null;
  try {
    res = requestFn(tp);
  } catch (e) {
    exceptionMessage = e && e.message ? String(e.message) : String(e);
    res = null;
  }
  const durationMs = Date.now() - t0;
  // isTimeoutError(res) covers the actual k6 default behavior for a real
  // network/response timeout: res.status is 0 and NO exception is thrown
  // at all. The rare k6-internal-exception path (e.g. a malformed URL) is
  // a distinct failure mode and must not be folded into "timeout" just
  // because it also has no res -- only classify it as a timeout if the
  // exception's own message says so.
  const exceptionWasTimeout =
    exceptionMessage !== null && /timeout|deadline exceeded/i.test(exceptionMessage);
  const timedOut = exceptionWasTimeout || isTimeoutError(res);

  const httpStatus = res ? res.status : 0;
  const expectedSubstring = STAGE_BODY_ASSERTIONS[stageName];
  // bodyOk defaults to false (not true) when a body assertion is expected
  // for this stage -- an empty/missing body must fail that assertion, not
  // vacuously pass it. Stages with no expected substring are unaffected.
  let bodyOk = !expectedSubstring;
  if (expectedSubstring) {
    bodyOk = !!res && !!res.body && res.body.indexOf(expectedSubstring) !== -1;
  }
  const success =
    !!res && res.status >= 200 && res.status < 400 && bodyOk && !timedOut;
  const assertions = {
    expected_body_substring: expectedSubstring || null,
    body_assertion_passed: expectedSubstring ? bodyOk : null,
  };

  const record = {
    schema_version: 1,
    run_id: RUN_ID,
    profile: PROFILE_NAME,
    transaction_id: ctx.transactionId,
    stage: stageName,
    stage_seq: ctx.stageSeq,
    trace_id: ctx.traceId,
    span_id: spanId,
    traceparent: tp,
    method: res ? res.request.method : "UNKNOWN",
    url: res ? res.url : "UNKNOWN",
    http_status: httpStatus,
    success: success,
    timeout: timedOut,
    exception: exceptionMessage,
    duration_ms: durationMs,
    event_time: eventTimeStart,
    assertions: assertions,
  };
  emitRecord(record);
  ctx.stageSeq += 1;
  if (!success) ctx.transactionSuccess = false;
  if (timedOut) ctx.transactionTimeout = true;
  return res;
}

export default function () {
  const traceId = newTraceId();
  const transactionId = `${RUN_ID}-${__VU}-${__ITER}-${traceId.slice(0, 8)}`;
  const ctx = {
    traceId,
    transactionId,
    stageSeq: 0,
    transactionSuccess: true,
    transactionTimeout: false,
  };
  const transactionStart = nowIso();

  const params = { timeout: STAGE_TIMEOUT };

  // 1. browse: home page
  doStage(ctx, "browse_home", (tp) =>
    http.get(`${BASE_URL}/`, {
      headers: { traceparent: tp },
      tags: { stage: "browse_home" },
      ...params,
    })
  );

  // 2. browse: product detail
  const productId = PRODUCT_IDS[Math.floor(Math.random() * PRODUCT_IDS.length)];
  doStage(ctx, "browse_product", (tp) =>
    http.get(`${BASE_URL}/product/${productId}`, {
      headers: { traceparent: tp },
      tags: { stage: "browse_product" },
      ...params,
    })
  );

  // 3. cart: add to cart
  doStage(ctx, "cart_add", (tp) =>
    http.post(
      `${BASE_URL}/cart`,
      { product_id: productId, quantity: "1" },
      {
        headers: { traceparent: tp, "Content-Type": "application/x-www-form-urlencoded" },
        tags: { stage: "cart_add" },
        ...params,
      }
    )
  );

  // 4. cart: view cart
  doStage(ctx, "cart_view", (tp) =>
    http.get(`${BASE_URL}/cart`, {
      headers: { traceparent: tp },
      tags: { stage: "cart_view" },
      ...params,
    })
  );

  // 5. checkout: place order
  doStage(ctx, "checkout", (tp) =>
    http.post(
      `${BASE_URL}/cart/checkout`,
      {
        email: "sentrix-synthetic@example.com",
        street_address: "1600 Amphitheatre Parkway",
        zip_code: "94043",
        city: "Mountain View",
        state: "CA",
        country: "United States",
        credit_card_number: "4432-8015-6152-0454",
        credit_card_expiration_month: "1",
        credit_card_expiration_year: "2030",
        credit_card_cvv: "672",
      },
      {
        headers: { traceparent: tp, "Content-Type": "application/x-www-form-urlencoded" },
        tags: { stage: "checkout" },
        ...params,
      }
    )
  );

  transactionsTotal.add(1);
  if (!ctx.transactionSuccess) transactionsFailed.add(1);

  emitRecord({
    schema_version: 1,
    run_id: RUN_ID,
    profile: PROFILE_NAME,
    transaction_id: transactionId,
    stage: "TRANSACTION_SUMMARY",
    stage_seq: ctx.stageSeq,
    trace_id: traceId,
    span_id: null,
    traceparent: null,
    method: null,
    url: null,
    http_status: null,
    success: ctx.transactionSuccess,
    timeout: ctx.transactionTimeout,
    duration_ms: Date.now() - Date.parse(transactionStart),
    event_time: transactionStart,
    assertions: {},
  });
}
