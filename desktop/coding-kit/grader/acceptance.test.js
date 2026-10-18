'use strict';

// Hidden acceptance tests for the coding comparison. They check the final
// project against TASKS.md. Run them with check.js, never from inside a run
// folder, so Claude does not see them during a session.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const target = process.env.KIT_TARGET;
if (!target) {
  throw new Error('KIT_TARGET must point at a run folder');
}

function load(file) {
  return require(path.join(target, 'src', file));
}

function lib() {
  return load('inventory.js');
}

function report() {
  return load('report.js');
}

function clock() {
  let tick = 0;
  return () => `t${++tick}`;
}

// Task 1: the seeded bug

test('task 1: removing exactly the stock on hand leaves zero', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.removeItem('ABC-1001', 5);
  assert.equal(inventory.getStock('ABC-1001'), 0);
});

test('task 1: removing one more than the stock throws and changes nothing', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  assert.throws(() => inventory.removeItem('ABC-1001', 6));
  assert.equal(inventory.getStock('ABC-1001'), 5);
});

// Task 2: SKU and quantity validation

test('task 2: InvalidSkuError is exported and is an Error', () => {
  const { InvalidSkuError } = lib();
  assert.equal(typeof InvalidSkuError, 'function');
  assert.ok(new InvalidSkuError('x') instanceof Error);
});

test('task 2: invalid SKUs throw InvalidSkuError', () => {
  const { Inventory, InvalidSkuError } = lib();
  const inventory = new Inventory();
  for (const sku of ['abc-1234', 'ABC-123', 'ABCD-1234', 'AB-1234', 'ABC1234', '', 'ABC-12345']) {
    assert.throws(() => inventory.addItem(sku, 'Thing', 1), InvalidSkuError, sku);
  }
  inventory.addItem('ABC-1234', 'Thing', 1);
  assert.equal(inventory.getStock('ABC-1234'), 1);
});

test('task 2: quantities must be positive integers', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  for (const qty of [0, -1, 1.5]) {
    assert.throws(() => inventory.addItem('ABC-1001', 'Bolt', qty), Error, String(qty));
  }
  inventory.addItem('ABC-1001', 'Bolt', 3);
  for (const qty of [0, -2, 0.5]) {
    assert.throws(() => inventory.removeItem('ABC-1001', qty), Error, String(qty));
  }
  assert.equal(inventory.getStock('ABC-1001'), 3);
});

// Task 3: reservations

test('task 3: InsufficientStockError is exported and is an Error', () => {
  const { InsufficientStockError } = lib();
  assert.equal(typeof InsufficientStockError, 'function');
  assert.ok(new InsufficientStockError('x') instanceof Error);
});

test('task 3: reserve, release, available and reserved', () => {
  const { Inventory, InsufficientStockError } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 10);
  inventory.reserve('ABC-1001', 3);
  assert.equal(inventory.available('ABC-1001'), 7);
  assert.equal(inventory.reserved('ABC-1001'), 3);
  assert.equal(inventory.getStock('ABC-1001'), 10);
  assert.throws(() => inventory.reserve('ABC-1001', 8), InsufficientStockError);
  inventory.release('ABC-1001', 2);
  assert.equal(inventory.reserved('ABC-1001'), 1);
  assert.throws(() => inventory.release('ABC-1001', 5), Error);
  assert.equal(inventory.reserved('ABC-1001'), 1);
});

test('task 3: removeItem can only take available stock', () => {
  const { Inventory, InsufficientStockError } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 10);
  inventory.reserve('ABC-1001', 4);
  assert.throws(() => inventory.removeItem('ABC-1001', 7), InsufficientStockError);
  inventory.removeItem('ABC-1001', 6);
  assert.equal(inventory.getStock('ABC-1001'), 4);
  assert.equal(inventory.available('ABC-1001'), 0);
});

test('task 3: unknown SKUs throw, getStock returns 0', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  assert.throws(() => inventory.reserve('ZZZ-0000', 1), Error);
  assert.throws(() => inventory.release('ZZZ-0000', 1), Error);
  assert.throws(() => inventory.available('ZZZ-0000'), Error);
  assert.throws(() => inventory.reserved('ZZZ-0000'), Error);
  assert.equal(inventory.getStock('ZZZ-0000'), 0);
});

test('task 3: reserve and release need positive integers', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 10);
  for (const qty of [0, -1, 2.5]) {
    assert.throws(() => inventory.reserve('ABC-1001', qty), Error, String(qty));
  }
  inventory.reserve('ABC-1001', 2);
  for (const qty of [0, -1, 0.5]) {
    assert.throws(() => inventory.release('ABC-1001', qty), Error, String(qty));
  }
  assert.equal(inventory.reserved('ABC-1001'), 2);
});

// Task 4: onHand and the JSON shapes

test('task 4: toJSON rows are exactly { sku, name, onHand, reserved }', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 10);
  inventory.reserve('ABC-1001', 3);
  const rows = inventory.toJSON();
  assert.equal(rows.length, 1);
  assert.deepEqual(Object.keys(rows[0]).sort(), ['name', 'onHand', 'reserved', 'sku']);
  assert.deepEqual(rows[0], { sku: 'ABC-1001', name: 'Bolt', onHand: 10, reserved: 3 });
});

test('task 4: fromJSON reads the old shape', () => {
  const { Inventory } = lib();
  const inventory = Inventory.fromJSON([{ sku: 'ABC-1001', name: 'Bolt', quantity: 4 }]);
  assert.equal(inventory.getStock('ABC-1001'), 4);
  assert.equal(inventory.reserved('ABC-1001'), 0);
});

test('task 4: fromJSON restores the new shape with reservations', () => {
  const { Inventory } = lib();
  const inventory = Inventory.fromJSON([
    { sku: 'ABC-1001', name: 'Bolt', onHand: 9, reserved: 2 },
    { sku: 'ABC-1002', name: 'Nut', onHand: 3, reserved: 0 },
  ]);
  assert.equal(inventory.getStock('ABC-1001'), 9);
  assert.equal(inventory.available('ABC-1001'), 7);
  assert.equal(inventory.reserved('ABC-1002'), 0);
  const again = Inventory.fromJSON(inventory.toJSON());
  assert.deepEqual(
    [...again.toJSON()].sort((a, b) => a.sku.localeCompare(b.sku)),
    [...inventory.toJSON()].sort((a, b) => a.sku.localeCompare(b.sku)),
  );
});

// Task 5: history

test('task 5: every successful change is recorded with the injected clock', () => {
  const { Inventory } = lib();
  const inventory = new Inventory({ now: clock() });
  inventory.addItem('ABC-1001', 'Bolt', 10);
  inventory.reserve('ABC-1001', 3);
  inventory.release('ABC-1001', 1);
  inventory.removeItem('ABC-1001', 4);
  assert.deepEqual(inventory.history(), [
    { type: 'add', sku: 'ABC-1001', qty: 10, at: 't1' },
    { type: 'reserve', sku: 'ABC-1001', qty: 3, at: 't2' },
    { type: 'release', sku: 'ABC-1001', qty: 1, at: 't3' },
    { type: 'remove', sku: 'ABC-1001', qty: 4, at: 't4' },
  ]);
});

test('task 5: failed calls add no entry', () => {
  const { Inventory } = lib();
  const inventory = new Inventory({ now: clock() });
  inventory.addItem('ABC-1001', 'Bolt', 2);
  assert.throws(() => inventory.removeItem('ABC-1001', 5));
  assert.throws(() => inventory.reserve('ABC-1001', 5));
  assert.throws(() => inventory.addItem('bad', 'Bolt', 1));
  assert.equal(inventory.history().length, 1);
});

test('task 5: history(sku) filters and history() returns a copy', () => {
  const { Inventory } = lib();
  const inventory = new Inventory({ now: clock() });
  inventory.addItem('ABC-1001', 'Bolt', 2);
  inventory.addItem('ABC-1002', 'Nut', 3);
  inventory.addItem('ABC-1001', 'Bolt', 1);
  assert.deepEqual(
    inventory.history('ABC-1001').map((entry) => entry.at),
    ['t1', 't3'],
  );
  const copy = inventory.history();
  copy.push({ type: 'add', sku: 'X', qty: 1, at: 'x' });
  copy.length = 0;
  assert.equal(inventory.history().length, 3);
});

test('task 5: the default clock gives ISO timestamps', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 1);
  const [entry] = inventory.history();
  assert.equal(typeof entry.at, 'string');
  assert.ok(!Number.isNaN(Date.parse(entry.at)), entry.at);
});

test('task 5: fromJSON records no history and passes options on', () => {
  const { Inventory } = lib();
  const inventory = Inventory.fromJSON(
    [{ sku: 'ABC-1001', name: 'Bolt', onHand: 5, reserved: 1 }],
    { now: () => 'fixed' },
  );
  assert.deepEqual(inventory.history(), []);
  inventory.addItem('ABC-1001', 'Bolt', 1);
  assert.equal(inventory.history()[0].at, 'fixed');
});

// Task 6: CSV

test('task 6: toCsv writes a sorted table with a trailing newline', () => {
  const { Inventory } = lib();
  const { toCsv } = report();
  const inventory = new Inventory();
  inventory.addItem('ABC-1002', 'Nut', 7);
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.reserve('ABC-1002', 2);
  assert.equal(
    toCsv(inventory),
    'sku,name,onHand,reserved,available\nABC-1001,Bolt,5,0,5\nABC-1002,Nut,7,2,5\n',
  );
});

test('task 6: toCsv quotes commas, quotes and newlines', () => {
  const { Inventory } = lib();
  const { toCsv } = report();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt, large', 1);
  inventory.addItem('ABC-1002', 'Say "hi"', 2);
  inventory.addItem('ABC-1003', 'Two\nlines', 3);
  assert.equal(
    toCsv(inventory),
    'sku,name,onHand,reserved,available\n' +
      'ABC-1001,"Bolt, large",1,0,1\n' +
      'ABC-1002,"Say ""hi""",2,0,2\n' +
      'ABC-1003,"Two\nlines",3,0,3\n',
  );
});

test('task 6: toCsv of an empty inventory is just the header', () => {
  const { Inventory } = lib();
  const { toCsv } = report();
  assert.equal(toCsv(new Inventory()), 'sku,name,onHand,reserved,available\n');
});

test('task 6: formatReport still lists every item', () => {
  const { Inventory } = lib();
  const { formatReport } = report();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 5);
  inventory.addItem('ABC-1002', 'Nut', 7);
  const text = formatReport(inventory);
  assert.equal(typeof text, 'string');
  assert.match(text, /ABC-1001/);
  assert.match(text, /ABC-1002/);
});

// Task 7: low stock

test('task 7: lowStock uses available stock, a default of 5 and the sort order', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1003', 'C', 4);
  inventory.addItem('ABC-1001', 'A', 4);
  inventory.addItem('ABC-1002', 'B', 9);
  inventory.addItem('ABC-1004', 'D', 6);
  inventory.reserve('ABC-1002', 7);
  inventory.addItem('ABC-1005', 'E', 5);
  assert.deepEqual(inventory.lowStock(), ['ABC-1002', 'ABC-1001', 'ABC-1003', 'ABC-1005']);
  assert.deepEqual(inventory.lowStock(2), ['ABC-1002']);
  assert.deepEqual(inventory.lowStock(10), [
    'ABC-1002',
    'ABC-1001',
    'ABC-1003',
    'ABC-1005',
    'ABC-1004',
  ]);
});

// Task 8: Map and bulkAdd

test('task 8: items are kept in a Map', () => {
  const { Inventory } = lib();
  const inventory = new Inventory();
  inventory.addItem('ABC-1001', 'Bolt', 1);
  assert.ok(inventory.items instanceof Map);
});

test('task 8: bulkAdd adds every row and records history', () => {
  const { Inventory } = lib();
  const inventory = new Inventory({ now: clock() });
  inventory.addItem('ABC-1001', 'Bolt', 1);
  inventory.bulkAdd([
    { sku: 'ABC-1001', name: 'Bolt', qty: 2 },
    { sku: 'ABC-1002', name: 'Nut', qty: 3 },
  ]);
  assert.equal(inventory.getStock('ABC-1001'), 3);
  assert.equal(inventory.getStock('ABC-1002'), 3);
  assert.deepEqual(
    inventory.history().map((entry) => [entry.type, entry.sku, entry.qty]),
    [
      ['add', 'ABC-1001', 1],
      ['add', 'ABC-1001', 2],
      ['add', 'ABC-1002', 3],
    ],
  );
});

test('task 8: bulkAdd with a bad SKU throws InvalidSkuError and changes nothing', () => {
  const { Inventory, InvalidSkuError } = lib();
  const inventory = new Inventory({ now: clock() });
  inventory.addItem('ABC-1001', 'Bolt', 1);
  assert.throws(
    () =>
      inventory.bulkAdd([
        { sku: 'ABC-1002', name: 'Nut', qty: 3 },
        { sku: 'bad', name: 'Oops', qty: 1 },
        { sku: 'ABC-1003', name: 'Washer', qty: 2 },
      ]),
    InvalidSkuError,
  );
  assert.equal(inventory.getStock('ABC-1002'), 0);
  assert.equal(inventory.getStock('ABC-1003'), 0);
  assert.equal(inventory.history().length, 1);
});

test('task 8: bulkAdd with a bad qty throws and changes nothing', () => {
  const { Inventory } = lib();
  const inventory = new Inventory({ now: clock() });
  assert.throws(
    () =>
      inventory.bulkAdd([
        { sku: 'ABC-1002', name: 'Nut', qty: 3 },
        { sku: 'ABC-1003', name: 'Washer', qty: 1.5 },
      ]),
    Error,
  );
  assert.equal(inventory.getStock('ABC-1002'), 0);
  assert.deepEqual(inventory.history(), []);
});

// Tasks 9 and 10: tests and documentation

test('task 9: the project has more tests than it started with', () => {
  const dir = path.join(target, 'test');
  const files = fs.readdirSync(dir).filter((name) => name.endsWith('.js'));
  const text = files.map((name) => fs.readFileSync(path.join(dir, name), 'utf8')).join('\n');
  const count = (text.match(/\btest\s*\(|\bit\s*\(/g) || []).length;
  assert.ok(count >= 15, `found ${count} test calls`);
});

test('task 10: the README documents the public API', () => {
  const text = fs.readFileSync(path.join(target, 'README.md'), 'utf8');
  for (const word of ['reserve', 'release', 'available', 'lowStock', 'bulkAdd', 'history', 'toCsv', 'InvalidSkuError', 'InsufficientStockError']) {
    assert.ok(text.includes(word), `README does not mention ${word}`);
  }
});
