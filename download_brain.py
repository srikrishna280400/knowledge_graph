import os
from groq import Groq

# 1. Setup
api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise SystemExit("Missing GROQ_API_KEY env var")
client = Groq(api_key=api_key)


# 2. Your specific output ID
OUTPUT_FILE_ID = "file_01ke24hap7ezpsvzne4m69tkwh" 

def download_results():
    print(f"📥 Downloading your 4,945 Cognitive Maps...")
    
    try:
        # Get the response object
        response = client.files.content(OUTPUT_FILE_ID)
        
        # .read() extracts the actual bytes from the BinaryAPIResponse
        data = response.read()
        
        # We save it as a local JSONL file
        with open("raw_brain_results.jsonl", "wb") as f:
            f.write(data)
            
        print("\n✅ SUCCESS! 'raw_brain_results.jsonl' is now on your D: drive.")
        print(f"File size: {len(data) / 1024:.2f} KB")

    except Exception as e:
        print(f"❌ Download failed: {e}")

if __name__ == "__main__":
    download_results()