import os
import argparse
from typing import Optional
import pandas as pd


def main(csv_path: Optional[str] = None):
    # default to JP Card Data; check common filename variants next to this script
    if csv_path is None:
        base_dir = os.path.dirname(__file__)
        candidates = [
            os.path.join(base_dir, 'JP_Card_Data.csv'),
            os.path.join(base_dir, 'JP Card Data.csv'),
            os.path.join(base_dir, 'JP_Card_Data.csv'.lower()),
        ]
        for p in candidates:
            if os.path.exists(p):
                csv_path = p
                break
        if csv_path is None:
            # fallback to the underscore variant for clearer error message
            csv_path = candidates[0]

    if not os.path.exists(csv_path):
        print(f'Error: CSV file not found at {csv_path}')
        return

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        print(f'Error reading CSV: {exc}')
        return

    print('--- CSV head (first 5 rows) ---')
    print(df.head(5).to_string(index=False))

    # detect a suitable "type" column (English 'Type' or Japanese 'タイプ' or similar)
    cols = list(df.columns)
    type_col = None
    for c in cols:
        cl = c.lower()
        if cl == 'type' or 'type' in cl or 'タイプ' in c:
            type_col = c
            break

    if type_col is None:
        print('\nError: No column matching "Type" found in CSV. Available columns:')
        print(', '.join(cols))
        return

    print(f'\n--- Counts for column: {type_col} ---')
    print(df[type_col].value_counts(dropna=False).to_string())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Load JP Card Data CSV and show summary')
    parser.add_argument('csv', nargs='?', help='Path to JP Card Data.csv (optional)')
    args = parser.parse_args()
    main(args.csv)
