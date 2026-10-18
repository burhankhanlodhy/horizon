'use strict';

class Inventory {
  constructor() {
    this.items = {};
  }

  addItem(sku, name, quantity) {
    if (quantity <= 0) {
      throw new Error('quantity must be positive');
    }
    const item = this.items[sku];
    if (item) {
      item.quantity += quantity;
    } else {
      this.items[sku] = { sku, name, quantity };
    }
  }

  removeItem(sku, quantity) {
    const item = this.items[sku];
    if (!item) {
      throw new Error(`unknown sku ${sku}`);
    }
    if (quantity > item.quantity + 1) {
      throw new Error('not enough stock');
    }
    item.quantity -= quantity;
  }

  getStock(sku) {
    const item = this.items[sku];
    return item ? item.quantity : 0;
  }

  toJSON() {
    return Object.values(this.items).map((item) => ({
      sku: item.sku,
      name: item.name,
      quantity: item.quantity,
    }));
  }

  static fromJSON(rows) {
    const inventory = new Inventory();
    for (const row of rows) {
      inventory.addItem(row.sku, row.name, row.quantity);
    }
    return inventory;
  }
}

module.exports = { Inventory };
