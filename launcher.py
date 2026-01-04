import os
from groq import Groq

# --- CONFIGURATION ---
# Replace with your actual key or ensure it's in your Environment Variables
api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise SystemExit("Missing GROQ_API_KEY env var")
client = Groq(api_key=api_key)

BATCH_FILE_PATH = "cognitive_map_batch.jsonl"

def launch_batch_job():
    print("📤 Step 1: Uploading your file to Groq...")
    
    # 1. Upload the file
    uploaded_file = client.files.create(
        file=open(BATCH_FILE_PATH, "rb"),
        purpose="batch"
    )
    file_id = uploaded_file.id
    print(f"✅ File uploaded successfully! File ID: {file_id}")

    print("🚀 Step 2: Kickstarting the 4,945-link Batch Job...")

    # 2. Start the batch job
    batch_job = client.batches.create(
        input_file_id=file_id,
        endpoint="/v1/chat/completions",
        completion_window="24h" # Gives Groq time to process cheaply
    )

    print("\n--- BATCH LAUNCHED ---")
    print(f"Batch ID: {batch_job.id}")
    print(f"Status:   {batch_job.status}")
    print("----------------------")
    print("👉 SAVE THIS BATCH ID. You will need it to download your results.")

if __name__ == "__main__":
    launch_batch_job()