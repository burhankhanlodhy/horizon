'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Inventory, InvalidSkuError, InsufficientStockError } = require('../src/inventory');
const { formatReport, toCsv } = require('../src/report');

const make = () => new Inventory({ now: () => 'now' });

test('add counts stock', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.equal(inventory.getStock('ABC-1001'), 5);
});

test('remove reduces stock', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.removeItem('ABC-1001', 5);
  assert.equal(inventory.getStock('ABC-1001'), 0);
});

test('remove too much throws', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.throws(() => inventory.removeItem('ABC-1001', 6), InsufficientStockError);
});

test('bad sku throws', () => {
  assert.throws(() => make().addItem('abc', 'x', 1), InvalidSkuError);
});

test('bad qty throws', () => {
  assert.throws(() => make().addItem('ABC-1001', 'x', 1.5));
});

test('reserve lowers available', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.reserve('ABC-1001', 2);
  assert.equal(inventory.available('ABC-1001'), 3);
});

test('release returns stock', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.reserve('ABC-1001', 2);
  inventory.release('ABC-1001', 2);
  assert.equal(inventory.reserved('ABC-1001'), 0);
});

test('over-release throws', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.throws(() => inventory.release('ABC-1001', 1));
});

test('toJSON shape', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.deepEqual(inventory.toJSON(), [{ sku: 'ABC-1001', name: 'Bolt', onHand: 5, reserved: 0 }]);
});

test('fromJSON old shape', () => {
  assert.equal(Inventory.fromJSON([{ sku: 'ABC-1001', name: 'Bolt', quantity: 2 }]).getStock('ABC-1001'), 2);
});

test('history records changes', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.deepEqual(inventory.history(), [{ type: 'add', sku: 'ABC-1001', qty: 5, at: 'now' }]);
});

test('csv export', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt, big', 5);
  assert.equal(toCsv(inventory), 'sku,name,onHand,reserved,available\nABC-1001,"Bolt, big",5,0,5\n');
});

test('report lists items', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.match(formatReport(inventory), /ABC-1001/);
});

test('low stock', () => {
  const inventory = make();
  inventory.addItem('ABC-1001', 'Bolt', 2);
  inventory.addItem('ABC-1002', 'Nut', 9);
  assert.deepEqual(inventory.lowStock(), ['ABC-1001']);
});

test('bulkAdd is all or nothing', () => {
  const inventory = make();
  assert.throws(() => inventory.bulkAdd([{ sku: 'ABC-1001', name: 'a', qty: 1 }, { sku: 'x', name: 'b', qty: 1 }]));
  assert.equal(inventory.getStock('ABC-1001'), 0);
});

test('bulkAdd adds rows', () => {
  const inventory = make();
  inventory.bulkAdd([{ sku: 'ABC-1001', name: 'a', qty: 1 }]);
  assert.equal(inventory.getStock('ABC-1001'), 1);
});
