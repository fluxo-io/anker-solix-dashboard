import test from 'node:test';
import assert from 'node:assert/strict';
import {createNotificationConsumer} from '../src/notification-events.mjs';

const event = (eventId, level = 'error') => ({eventId, level, message: 'Synthetische Meldung'});

test('polling consumes an event once and keeps acknowledgement bounded', () => {
  const sent = [];
  const consume = createNotificationConsumer(value => sent.push(value));
  const state = consume([event('outage'), null, null], {});
  assert.equal(consume([event('outage'), null, null], state), null);
  assert.equal(sent.length, 1);
  assert.deepEqual(Object.keys(state), ['filter', 'data', 'snapshot']);
});

test('separate actions with identical text and independent channels remain visible', () => {
  const sent = [];
  const consume = createNotificationConsumer(value => sent.push(value));
  const first = consume([event('filters'), event('reload:1', 'success'), event('details')]);
  consume([event('filters'), event('reload:2', 'success'), event('details')], first);
  assert.equal(sent.length, 4);
  assert.equal(new Set(sent.map(value => value.eventId)).size, 4);
});

test('recovery resets the channel and a later outage gets a new delivery identity', () => {
  const sent = [];
  const consume = createNotificationConsumer(value => sent.push(value));
  const failed = consume([event('outage'), null, null]);
  const recovered = consume([null, null, null], failed);
  consume([event('outage'), null, null], recovered);
  assert.equal(sent.length, 2);
  assert.notEqual(sent[0].eventId, sent[1].eventId);
});

test('malformed and oversized events are not delivered', () => {
  const sent = [];
  const consume = createNotificationConsumer(value => sent.push(value));
  const invalid = [null, {}, event('', 'error'), event('x', 'bogus'),
    {...event('x'), message: {html: '<script>'}}, {...event('x'), message: 'x'.repeat(1001)},
    {...event('x'), message: ''}, event('x'.repeat(129))];
  for (const value of invalid) consume([value, value, value]);
  assert.deepEqual(sent, []);
  consume([{...event('safe'), message: '<b>Text bleibt Text</b>'}]);
  assert.equal(sent[0].message, '<b>Text bleibt Text</b>');
});
