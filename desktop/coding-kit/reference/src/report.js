'use strict';

function formatReport(inventory) {
  return inventory
    .toJSON()
    .map((row) => `${row.sku}  ${row.name}  ${row.onHand}`)
    .join('\n');
}

function field(value) {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function toCsv(inventory) {
  const rows = [...inventory.toJSON()].sort((a, b) => (a.sku < b.sku ? -1 : a.sku > b.sku ? 1 : 0));
  const lines = ['sku,name,onHand,reserved,available'];
  for (const row of rows) {
    lines.push(
      [row.sku, row.name, row.onHand, row.reserved, row.onHand - row.reserved].map(field).join(','),
    );
  }
  return `${lines.join('\n')}\n`;
}

module.exports = { formatReport, toCsv };
