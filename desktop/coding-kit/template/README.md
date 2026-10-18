# inventory

A small stock-keeping library for Node.js. It has no dependencies.

```js
const { Inventory } = require('./src/inventory');

const inventory = new Inventory();
inventory.addItem('ABC-1001', 'Bolt', 5);
inventory.getStock('ABC-1001'); // 5
```

Run the tests with `npm test`.
