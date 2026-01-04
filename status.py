from groq import Groq

# 1. Setup - Use your key
api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise SystemExit("Missing GROQ_API_KEY env var")
client = Groq(api_key=api_key)

# 2. Use the ID from your previous terminal
BATCH_ID = "batch_01ke2419e9ejrbtwyr57dgx9wm"

def check_progress():
    print(f"📡 Fetching update for {BATCH_ID}...")
    
    try:
        # We use .retrieve() which is the most stable method
        job = client.batches.retrieve(BATCH_ID)
        
        print(f"\n--- [ BATCH STATUS ] ---")
        print(f"ID:       {job.id}")
        print(f"Status:   {job.status.upper()}")
        
        # Check if Groq has started counting yet
        if job.request_counts:
            done = job.request_counts.completed
            total = job.request_counts.total
            failed = job.request_counts.failed
            
            print(f"Progress: {done} / {total} links processed")
            if failed > 0:
                print(f"⚠️ Errors: {failed} links couldn't be analyzed")
        
        print("------------------------")
        
        if job.status == "completed":
            print("\n✅ YOUR BRAIN IS READY!")
            print(f"Result File ID: {job.output_file_id}")
            print("You can now download the results.")
        elif job.status == "validating":
            print("\n⏳ Groq is still checking your file. Try again in 5 minutes.")
        elif job.status == "in_progress":
            print("\n🧠 AI is actively analyzing your 4,945 links...")

    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    check_progress()