'use strict';

function formatReport(inventory) {
  return inventory
    .toJSON()
    .map((row) => `${row.sku}  ${row.name}  ${row.quantity}`)
    .join('\n');
}

module.exports = { formatReport };
