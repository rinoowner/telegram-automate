import sys
from database import add_trial_keys

def main():
    print("=== Add Trial Keys to Database ===")
    print("Enter the trial keys one by one. Press Enter on an empty line to finish.")
    
    keys_to_add = []
    while True:
        key = input("Enter Key: ").strip()
        if not key:
            break
        keys_to_add.append(key)
        
    if keys_to_add:
        added = add_trial_keys(keys_to_add)
        print(f"Successfully added {added} new unique keys to the database out of {len(keys_to_add)} entered.")
    else:
        print("No keys added.")

if __name__ == "__main__":
    init_try = False
    try:
        from database import init_db
        init_db()
    except Exception as e:
        pass
    main()
