const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const path = require('node:path');
const app = fs.readFileSync(
  path.resolve(__dirname, '../../mvp_v2/ui/app.js'),
  'utf8',
);
const queueSource = app.slice(
  app.indexOf('function queueFormalScriptCellSave('),
  app.indexOf('\nfunction formalScriptDate('),
);
const proposalInvalidationSource = app.slice(
  app.indexOf('function invalidateFormalScriptProposal('),
  app.indexOf('\nfunction renderFormalScriptRevisionProposal('),
);
const imageProposalInvalidationSource = app.slice(
  app.indexOf('function invalidateFormalImageVersionProposal('),
  app.indexOf('\nfunction captureFormalImageInputs('),
);

function harness({holdFirst = false} = {}) {
  let n = 0;
  let release;
  const requests = [];
  const receipts = new Map();
  const saved = {
    project_version: 5,
    revision: 0,
    shots: [
      {shot_no: '1', shot_id: 'stable1', subtitle: 'A'},
      {shot_no: '2', shot_id: 'stable2', subtitle: 'D'},
    ],
  };
  const clone = (value) => structuredClone(value);
  const state = {
    formalScript: clone(saved),
    formalScriptDirtyCells: new Map(),
    formalScriptSaveRetryPending: null,
    formalScriptSaveInFlightPending: null,
    formalScriptSavePromise: null,
    formalScriptSaveGeneration: 0,
    formalScriptProjectId: 'isolated',
    formalScriptDiscarding: false,
    formalScriptProposalRequest: 0,
    formalScriptRevisionProposal: {task_id: 'stale-proposal'},
    formalScriptScopeTask: {task_id: 'stale-scope'},
    formalImageInputRevision: 0,
    formalImageVersionProposal: {proposal_id: 'stale-image-proposal'},
  };
  const ctx = vm.createContext({
    state,
    Number,
    String,
    JSON,
    Map,
    window: {clearTimeout() {}, setTimeout() { return 1; }},
    elements: {
      formalScriptRevisionProposal: {}, formalScriptScope: {},
      formalScriptSaveProposal: {}, formalScriptInstructionSubmit: {},
      formalScriptSave: {},
      formalImageVersionProposal: {replaceChildren() {}},
    },
    formalScriptShotIdentity: (shot) => shot.shot_id,
    formalScriptCellValue: (control) => control.value,
    formalRequestKey: () => `operation-${++n}`,
    syncFormalScriptSaveControls() {},
    renderCreativeOutline() {},
    syncCreativeVisibility() {},
    showToast() {},
    setFormalScriptStatus(_state, message) { state.status = message; },
    formalScriptPath: () => '/table',
    formalError: (error) => error.message,
    api: async (_path, options) => {
      const body = JSON.parse(options.body);
      requests.push(body);
      if (holdFirst && requests.length === 1) {
        await new Promise((resolve) => { release = resolve; });
      }
      const prior = receipts.get(body.idempotency_key);
      if (prior) {
        if (prior.request !== JSON.stringify(body)) throw Error('IDEMPOTENCY_CONFLICT');
        return clone(saved);
      }
      if (body.expected_revision !== saved.revision) throw Error('DRAFT_REVISION_CONFLICT');
      const shot = saved.shots.find((item) => item.shot_id === body.shot_id);
      if (shot[body.field] === body.value) throw Error('DRAFT_NOT_CHANGED');
      shot[body.field] = body.value;
      saved.revision += 1;
      receipts.set(body.idempotency_key, {request: JSON.stringify(body)});
      return clone(saved);
    },
  });
  vm.runInContext(`${proposalInvalidationSource}\n${imageProposalInvalidationSource}\n${queueSource}`, ctx);
  return {
    state,
    requests,
    saved: () => saved,
    release: () => release(),
    input(no, value) {
      const control = {
        dataset: {
          scriptShot: String(no),
          scriptShotKey: `stable${no}`,
          scriptField: 'subtitle',
        },
        value,
      };
      control.closest = () => control;
      ctx.queueFormalScriptCellSave({target: control});
    },
    flush: () => ctx.flushFormalScriptChanges(),
  };
}

async function main() {
  const results = [];
  const run = async (id, fn) => {
    try {
      await fn();
      results.push(`STEP_PASS|${id}`);
    } catch (error) {
      results.push(`STEP_FAIL|${id}|${error.message}`);
    }
  };

  await run('Q1-operation-key-is-per-save-operation', async () => {
    const h = harness();
    h.input(1, 'B'); await h.flush();
    assert.equal(h.state.formalScriptRevisionProposal, null);
    assert.equal(h.state.formalScriptScopeTask, null);
    assert.equal(h.state.formalImageVersionProposal, null);
    h.input(1, 'C'); await h.flush();
    h.input(1, 'B'); await h.flush();
    assert.equal(h.saved().shots[0].subtitle, 'B');
    assert.deepEqual(h.requests.map((item) => item.idempotency_key), [
      'operation-1', 'operation-2', 'operation-3',
    ]);
  });

  await run('Q2-unsent-revert-does-not-block-other-cell', async () => {
    const h = harness();
    h.input(1, 'B'); h.input(2, 'E'); h.input(1, 'A');
    await h.flush();
    assert.equal(h.saved().shots[0].subtitle, 'A');
    assert.equal(h.saved().shots[1].subtitle, 'E');
    assert.deepEqual(h.requests.map((item) => item.shot_no), ['2']);
  });

  await run('Q3-inflight-old-value-cancels-stale-queued-value', async () => {
    const h = harness({holdFirst: true});
    h.input(1, 'B');
    const pending = h.flush();
    h.input(1, 'C'); h.input(1, 'B'); h.release();
    await pending;
    assert.equal(h.saved().shots[0].subtitle, 'B');
    assert.deepEqual(h.requests.map((item) => item.value), ['B']);
  });

  process.stdout.write(`${results.join('\n')}\n`);
  if (results.some((item) => item.startsWith('STEP_FAIL'))) process.exitCode = 1;
}

main();
