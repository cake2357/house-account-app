ALTER TABLE receipt_items ADD COLUMN amount_yen INTEGER
    CHECK (amount_yen IS NULL OR typeof(amount_yen) = 'integer');
ALTER TABLE receipts ADD COLUMN total_amount_yen INTEGER
    CHECK (total_amount_yen IS NULL OR (typeof(total_amount_yen) = 'integer' AND total_amount_yen >= 0));
