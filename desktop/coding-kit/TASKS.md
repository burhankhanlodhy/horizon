# The 10 tasks

Paste these into **one** Code-tab session, one at a time, in order. Wait until
Claude has completely finished a task before you paste the next one. Copy each
task exactly, without the "Task N" heading.

If Claude asks you a question, reply only: `Use your best judgment and continue.`
If Claude stops before finishing, reply only: `Continue.`
Do not give any other help, and do not run /compact or /clear.

---

## Task 1

This folder is a small Node.js inventory library (CommonJS, no dependencies). Run the tests with `npm test`. One test fails. Find the bug and fix it. Do not change the tests.

## Task 2

Add SKU validation to `addItem`. A valid SKU is three uppercase letters, a hyphen and four digits, for example `ABC-1234`. For an invalid SKU, `addItem` must throw an `InvalidSkuError`. Define `InvalidSkuError` as a subclass of `Error` and export it from `src/inventory.js` next to `Inventory`. Also, the quantity passed to `addItem` and `removeItem` must be a positive integer; throw an `Error` otherwise.

## Task 3

Add reservations to `Inventory`:
- `reserve(sku, qty)` sets stock aside and `release(sku, qty)` gives it back.
- `available(sku)` returns the stock on hand minus the reserved amount, and `reserved(sku)` returns the reserved amount.
- Reserving more than is available throws an `InsufficientStockError`, a subclass of `Error` exported from `src/inventory.js`.
- Releasing more than is reserved throws an `Error`.
- `removeItem` may only remove available stock; trying to remove more throws an `InsufficientStockError`.
- `getStock` still returns the total on hand, including reserved stock.
- `reserve`, `release`, `available` and `reserved` throw an `Error` for an unknown SKU; `getStock` still returns 0 for one.
- `qty` must be a positive integer in `reserve` and `release`; throw an `Error` otherwise.

## Task 4

Rename the item field `quantity` to `onHand` everywhere in the code. Each row returned by `toJSON()` must now be exactly `{ sku, name, onHand, reserved }`. `Inventory.fromJSON(rows)` must accept both the new row shape and the old shape that has `quantity` instead of `onHand` (old rows have no `reserved`; treat it as 0), and it must restore reservations from the new shape.

## Task 5

Add a movement history:
- The constructor takes an optional options object, `new Inventory({ now })`, where `now` is a function that returns a timestamp. The default is `() => new Date().toISOString()`.
- Every successful `addItem`, `removeItem`, `reserve` and `release` appends `{ type, sku, qty, at }`, where `type` is `'add'`, `'remove'`, `'reserve'` or `'release'` and `at` is the value returned by `now()`.
- `history()` returns a copy of all entries, oldest first. `history(sku)` returns only the entries for that SKU.
- A call that throws adds no entry.
- `Inventory.fromJSON(rows, options)` passes `options` to the constructor and records no history while loading.

## Task 6

In `src/report.js`, add and export `toCsv(inventory)`. It returns CSV text with the header `sku,name,onHand,reserved,available`, then one row per item sorted by SKU. Lines are separated by `\n`, and the text ends with `\n`. Put a field in double quotes when it contains a comma, a double quote or a newline, and double any double quote inside it. Keep `formatReport` working.

## Task 7

Add `lowStock(threshold = 5)` to `Inventory`. It returns the SKUs whose available quantity is less than or equal to `threshold`, sorted by available quantity from lowest to highest, then by SKU.

## Task 8

Refactor `Inventory` to keep its items in a `Map` stored in the `items` property, with no change in behaviour. Then add `bulkAdd(rows)`, where each row is `{ sku, name, qty }`. It is all-or-nothing: if any row is invalid (a bad SKU, or a qty that is not a positive integer), it throws the same error `addItem` would throw and changes nothing at all, neither stock nor history. Otherwise it adds every row and records history exactly as `addItem` does.

## Task 9

Write tests in the `test/` folder for everything added in this session, and make sure `npm test` passes.

## Task 10

Update `README.md` with a short usage guide and a list of the public API: the classes, their methods, the error types and `toCsv`. Then run `npm test` one last time and give me a one-paragraph summary of what changed.
