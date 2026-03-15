import asyncio
from pyrogram import Client

async def main():
    print("--- Rino Mods: Session String Generator ---")
    api_id = input("Enter API_ID: ")
    api_hash = input("Enter API_HASH: ")
    
    # Using a unique session name instead of :memory: to avoid Windows file lock issues
    session_name = "temp_session_gen"
    app = Client(session_name, api_id=api_id, api_hash=api_hash)
    
    async with app:
        session_string = await app.export_session_string()
        print("\n" + "="*50)
        print("YOUR SESSION STRING (COPY THIS):")
        print("="*50)
        print(session_string)
        print("="*50)
        print("\nPut this string in your Render Environment Variables as SESSION_STRING.")
    
    # Clean up the temporary session file
    if os.path.exists(f"{session_name}.session"):
        os.remove(f"{session_name}.session")

if __name__ == "__main__":
    asyncio.run(main())
