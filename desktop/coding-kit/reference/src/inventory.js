'use strict';

// Reference solution for TASKS.md. It proves the acceptance tests can be
// passed. It is never copied into a run folder.

const SKU = /^[A-Z]{3}-\d{4}$/;

class InvalidSkuError extends Error {}
class InsufficientStockError extends Error {}

function checkQty(qty) {
  if (!Number.isInteger(qty) || qty <= 0) {
    throw new Error('qty must be a positive integer');
  }
}

function checkSku(sku) {
  if (typeof sku !== 'string' || !SKU.test(sku)) {
    throw new InvalidSkuError(`invalid sku ${sku}`);
  }
}

class Inventory {
  constructor({ now = () => new Date().toISOString() } = {}) {
    this.items = new Map();
    this.now = now;
    this.entries = [];
  }

  record(type, sku, qty) {
    this.entries.push({ type, sku, qty, at: this.now() });
  }

  known(sku) {
    const item = this.items.get(sku);
    if (!item) {
      throw new Error(`unknown sku ${sku}`);
    }
    return item;
  }

  addItem(sku, name, qty) {
    checkSku(sku);
    checkQty(qty);
    const item = this.items.get(sku);
    if (item) {
      item.onHand += qty;
    } else {
      this.items.set(sku, { sku, name, onHand: qty, reserved: 0 });
    }
    this.record('add', sku, qty);
  }

  bulkAdd(rows) {
    for (const row of rows) {
      checkSku(row.sku);
      checkQty(row.qty);
    }
    for (const row of rows) {
      this.addItem(row.sku, row.name, row.qty);
    }
  }

  removeItem(sku, qty) {
    const item = this.known(sku);
    checkQty(qty);
    if (qty > item.onHand - item.reserved) {
      throw new InsufficientStockError('not enough available stock');
    }
    item.onHand -= qty;
    this.record('remove', sku, qty);
  }

  reserve(sku, qty) {
    const item = this.known(sku);
    checkQty(qty);
    if (qty > item.onHand - item.reserved) {
      throw new InsufficientStockError('not enough available stock');
    }
    item.reserved += qty;
    this.record('reserve', sku, qty);
  }

  release(sku, qty) {
    const item = this.known(sku);
    checkQty(qty);
    if (qty > item.reserved) {
      throw new Error('cannot release more than is reserved');
    }
    item.reserved -= qty;
    this.record('release', sku, qty);
  }

  getStock(sku) {
    const item = this.items.get(sku);
    return item ? item.onHand : 0;
  }

  available(sku) {
    const item = this.known(sku);
    return item.onHand - item.reserved;
  }

  reserved(sku) {
    return this.known(sku).reserved;
  }

  lowStock(threshold = 5) {
    return [...this.items.values()]
      .map((item) => ({ sku: item.sku, available: item.onHand - item.reserved }))
      .filter((row) => row.available <= threshold)
      .sort((a, b) => a.available - b.available || (a.sku < b.sku ? -1 : a.sku > b.sku ? 1 : 0))
      .map((row) => row.sku);
  }

  history(sku) {
    const list = sku === undefined ? this.entries : this.entries.filter((entry) => entry.sku === sku);
    return list.map((entry) => ({ ...entry }));
  }

  toJSON() {
    return [...this.items.values()].map((item) => ({
      sku: item.sku,
      name: item.name,
      onHand: item.onHand,
      reserved: item.reserved,
    }));
  }

  static fromJSON(rows, options) {
    const inventory = new Inventory(options);
    for (const row of rows) {
      const onHand = row.onHand !== undefined ? row.onHand : row.quantity;
      checkSku(row.sku);
      checkQty(onHand);
      inventory.items.set(row.sku, {
        sku: row.sku,
        name: row.name,
        onHand,
        reserved: row.reserved || 0,
      });
    }
    return inventory;
  }
}

module.exports = { Inventory, InvalidSkuError, InsufficientStockError };
