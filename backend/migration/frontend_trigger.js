// const EXTENSION_ID = "PASTE_YOUR_EXTENSION_ID_HERE";

// type ImportAction = "collect_linkedin" | "collect_reddit" | "collect_x";

// export async function triggerCollector(action: ImportAction) {
//   return new Promise((resolve, reject) => {
//     if (!window.chrome?.runtime?.sendMessage) {
//       reject(new Error("Chrome extension messaging is unavailable"));
//       return;
//     }

//     window.chrome.runtime.sendMessage(
//       EXTENSION_ID,
//       { action },
//       (response: any) => {
//         const err = window.chrome.runtime.lastError;
//         if (err) {
//           reject(new Error(err.message));
//           return;
//         }
//         if (!response?.ok) {
//           reject(new Error(response?.error || "Collector failed"));
//           return;
//         }
//         resolve(response);
//       }
//     );
//   });
// }


{/* <button onClick={() => triggerCollector("collect_linkedin")}>
  Import saved items from LinkedIn
</button>

<button onClick={() => triggerCollector("collect_reddit")}>
  Import saved items from Reddit
</button>

<button onClick={() => triggerCollector("collect_x")}>
  Import bookmarks from X
</button> */}
