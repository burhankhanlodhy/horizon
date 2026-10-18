'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Inventory } = require('../src/inventory');
const { formatReport } = require('../src/report');

test('adding stock is counted', () => {
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.equal(inventory.getStock('ABC-1001'), 5);
});

test('adding the same SKU twice accumulates', () => {
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.addItem('ABC-1001', 'Bolt', 3);
  assert.equal(inventory.getStock('ABC-1001'), 8);
});

test('removing stock reduces it', () => {
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.removeItem('ABC-1001', 2);
  assert.equal(inventory.getStock('ABC-1001'), 3);
});

test('removing more than is in stock throws', () => {
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.throws(() => inventory.removeItem('ABC-1001', 6));
  assert.equal(inventory.getStock('ABC-1001'), 5);
});

test('removing an unknown SKU throws', () => {
  const inventory = new Inventory();
  assert.throws(() => inventory.removeItem('ABC-9999', 1));
});

test('the report lists every item', () => {
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.addItem('ABC-1002', 'Nut', 7);
  const report = formatReport(inventory);
  assert.match(report, /ABC-1001/);
  assert.match(report, /ABC-1002/);
});
