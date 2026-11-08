import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const source = fs.readFileSync(path.join(__dirname, '../src/notifications.js'), 'utf8').replace(/^import .*;\n/gm, '');
function setup({lateHeader = false} = {}) {
  let header = lateHeader ? null : {bottom: 80, getBoundingClientRect() {return {bottom: this.bottom};}};
  const sent = [], timers = [], changes = {}, documentEvents = {}, windowEvents = {}, observers = [];
  const context = {
    notify: event => sent.push(event), initNotifications() {},
    createNotificationConsumer: () => () => null,
    sessionStorage: {getItem: () => null},
    document: {readyState: 'complete', querySelector: () => header,
      body: {style: {setProperty: (name, value) => {changes[name] = value;}}},
      addEventListener: (name, fn) => {documentEvents[name] = fn;}},
    window: {setTimeout: fn => timers.push(fn), addEventListener: (name, fn) => {windowEvents[name] = fn;}, dash_clientside: {no_update: 'unchanged'}},
    queueMicrotask,
    ResizeObserver: class {constructor(fn) {this.fn = fn; observers.push(this);} observe(element) {this.element = element;} disconnect() {this.disconnected = true;}},
  };
  vm.runInNewContext(source, context);
  return {sent, timers, changes, documentEvents, windowEvents, observers, context,
    header: () => header, insertHeader(bottom) {header = {bottom, getBoundingClientRect() {return {bottom: this.bottom};}};},
    flushTimers() {timers.splice(0).forEach(fn => fn());}};
}

test('native invalid fields produce one summary per submission even across microtasks', async () => {
  const app = setup();
  app.documentEvents.invalid();
  await Promise.resolve();
  app.documentEvents.invalid();
  await Promise.resolve();
  assert.equal(app.sent.length, 0);
  assert.equal(app.timers.length, 1);
  app.flushTimers();
  assert.equal(app.sent.length, 1);
  assert.equal(app.sent[0].level, 'error');
  app.documentEvents.invalid();
  app.flushTimers();
  assert.equal(app.sent.length, 2, 'a later deliberate attempt must be announced again');
});

test('toast position follows expanded headers and viewport/scroll changes', () => {
  const app = setup();
  assert.equal(app.changes['--notification-top'], '96px');
  app.header().bottom = 280;
  app.observers[0].fn();
  assert.equal(app.changes['--notification-top'], '296px');
  app.header().bottom = -70;
  app.windowEvents.scroll();
  assert.equal(app.changes['--notification-top'], '16px');
  app.header().bottom = 72;
  app.windowEvents.resize();
  assert.equal(app.changes['--notification-top'], '88px');
});

test('the first Dash event attaches to the header rendered after DOM readiness', () => {
  const app = setup({lateHeader: true});
  assert.equal(app.changes['--notification-top'], '16px');
  app.insertHeader(110);
  app.context.window.dash_clientside.fluxo.consumeNotifications(null, null, null, {});
  assert.equal(app.changes['--notification-top'], '126px');
  assert.equal(app.observers[0].element, app.header());
});
