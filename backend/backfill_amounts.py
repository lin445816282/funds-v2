#!/usr/bin/env python3
"""
Backfill order_amounts and order_history for historical dates.
Dates: 2026-01-01 to 2026-06-30 (approx 181 dates)
"""
import sqlite3, json, sys, os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simulate import (
    FUNDS_DB, STORE_NAMES, COLLECTION_TO_STORE, STORE_TO_COLLECTION,
    _load_order_numbers, save_order_amounts
)

def get_consensus_stores(date_str, mode):
    """Get consensus stores from sim_guides for a given date and mode."""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT result FROM sim_guides WHERE date=? AND json_extract(result, '$.mode')=? ORDER BY id DESC LIMIT 1",
        (date_str, mode)
    ).fetchone()
    db.close()
    if not row:
        return []
    result = json.loads(row["result"])
    consensus = result.get("consensus", [])
    stores = []
    for c in consensus:
        caps = c.get("caps", {})
        if not caps:
            continue
        max_cap = max(caps.values())
        if max_cap > 0:
            stores.append({
                "store": c["store"],
                "capital": max_cap,
                "votes": f"{c['votes']}/{c['out_of']}",
            })
    return stores

def backfill():
    db = sqlite3.connect(FUNDS_DB)
    
    # Get all dates that need backfilling (order_amounts missing)
    # order_amounts has dates starting from 2026-07-01
    # We need to fill 2026-01-01 to 2026-06-30
    start = datetime(2026, 1, 1)
    end = datetime(2026, 6, 30)
    
    # First, check which dates already have order_amounts
    existing_dates = set()
    for row in db.execute("SELECT DISTINCT date FROM order_amounts"):
        existing_dates.add(row[0])
    db.close()
    
    # Also check order_history dates with amounts
    db2 = sqlite3.connect(FUNDS_DB)
    existing_hist = set()
    for row in db2.execute("SELECT date FROM order_history WHERE amounts_json IS NOT NULL AND amounts_json != '' AND amounts_json != '{}'"):
        existing_hist.add(row[0])
    db2.close()
    
    print(f"Dates with order_amounts: {len(existing_dates)}")
    print(f"Dates with order_history amounts: {len(existing_hist)}")
    
    # Load numbers (will use latest available - auto fallback in _load_order_numbers)
    numbers = _load_order_numbers()
    print(f"Using order_numbers date: {numbers.get('date', 'N/A')}")
    print(f"Stores with numbers: {[k for k in numbers.keys() if k != 'date' and k != 'matched']}")
    
    # Process each date
    total_success = 0
    total_skip = 0
    total_error = 0
    
    d = start
    while d <= end:
        date_str = d.strftime("%Y-%m-%d")
        
        # Skip if already done
        action_date = (d + timedelta(days=1)).strftime("%Y-%m-%d")
        if action_date in existing_dates:
            total_skip += 1
            d += timedelta(days=1)
            continue
        
        try:
            # Get consensus for this date
            pos_stores = get_consensus_stores(date_str, "positive")
            neg_stores = get_consensus_stores(date_str, "negative")
            
            if not pos_stores and not neg_stores:
                print(f"  {date_str}: no consensus data")
                total_skip += 1
                d += timedelta(days=1)
                continue
            
            # Build stores_data for save_order_amounts
            stores_data = []
            for s in pos_stores:
                store_nums = numbers.get(s["store"], {})
                stores_data.append({
                    "store": s["store"],
                    "capital": s["capital"],
                    "mode": "positive",
                    "numbers": store_nums
                })
            for s in neg_stores:
                store_nums = numbers.get(s["store"], {})
                stores_data.append({
                    "store": s["store"],
                    "capital": s["capital"],
                    "mode": "negative",
                    "numbers": store_nums
                })
            
            if not stores_data:
                total_skip += 1
                d += timedelta(days=1)
                continue
            
            # Save
            result = save_order_amounts(date_str, stores_data)
            if result.get("ok"):
                total_success += 1
                if total_success % 10 == 0:
                    print(f"  ... {total_success} done, latest: {date_str} total={result.get('total')}")
            else:
                print(f"  {date_str}: FAILED - {result.get('error', 'unknown')}")
                total_error += 1
                
        except Exception as e:
            print(f"  {date_str}: ERROR - {e}")
            total_error += 1
        
        d += timedelta(days=1)
    
    print(f"\n=== Backfill complete ===")
    print(f"Success: {total_success}")
    print(f"Skipped (already exist): {total_skip}")
    print(f"Errors: {total_error}")

if __name__ == "__main__":
    backfill()
