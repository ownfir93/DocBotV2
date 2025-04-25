
document.addEventListener("DOMContentLoaded", () => {
    const chat = document.getElementById("chat");
    const chatForm = document.getElementById("chat-form");
    const questionInput = document.getElementById("question");
    const sendButton = document.getElementById("send");
    const stopButton = document.getElementById("stop");

    let controller = null; // For aborting fetch requests

    function displayMessage(sender, messageHtml) {
        const messageDiv = document.createElement("div");
        messageDiv.classList.add("message", sender); // 'user' or 'bot'

        const bubbleDiv = document.createElement("div");
        bubbleDiv.classList.add("bubble");
        bubbleDiv.innerHTML = messageHtml; // Use innerHTML to render links etc.

        messageDiv.appendChild(bubbleDiv);
        chat.appendChild(messageDiv);
        chat.scrollTop = chat.scrollHeight; // Scroll to bottom
    }

    chatForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const question = questionInput.value.trim();
        if (!question) return;

        // Display user message
        displayMessage("user", question);
        questionInput.value = ""; // Clear input field
        
        // --- MODIFIED: Hide input/send, show stop, add class ---
        questionInput.style.display = 'none';
        sendButton.style.display = 'none'; // Hide send button
        stopButton.style.display = 'inline-flex'; // Show stop button
        chatForm.classList.add('processing'); // Add class for centering
        // --- END MODIFIED ---

        // Display temporary "Working" message
        const thinkingMsg = document.createElement("div");
        thinkingMsg.classList.add("message", "bot", "temp-thinking"); // Add class to identify
        thinkingMsg.innerHTML = `<div class="bubble">Working on it... <span class="inline-spinner"></span></div>`;
        chat.appendChild(thinkingMsg);
        chat.scrollTop = chat.scrollHeight;

        // Abort previous request if any
        if (controller) {
             controller.abort();
        }
        controller = new AbortController();
        const signal = controller.signal;

        try {
            const response = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ question: question }),
                signal: signal, // Pass the abort signal
            });

            // --- Remove temporary message ---
            const tempMsg = chat.querySelector(".temp-thinking");
            if (tempMsg) {
                 tempMsg.remove();
            }
            // --- END Remove ---

            if (!response.ok) {
                let errorMsg = `HTTP error ${response.status}`;
                try {
                    const errData = await response.json();
                    errorMsg = errData.error || errData.message || errorMsg;
                } catch (e) { /* Ignore json parsing error */ }
                displayMessage("bot", `<p class="error">Error: ${errorMsg}</p>`);
                return; // Stop processing on error
            }

            const data = await response.json();

            // 1. Display main answer bubble
            displayMessage("bot", data.answer_html || "Sorry, I couldn't get a response.");

            // 2. Display Recommendation Card (if available) as a new element
            if (data.main_recommendation && data.main_recommendation.filename) {
                const recData = data.main_recommendation;
                const recDiv = document.createElement("div");
                // Add classes for styling - treat it like a message for spacing, but maybe unique class too
                recDiv.classList.add("message", "bot", "recommendation-container");

                let linkHTML = `<a href="${recData.link || '#'}" target="_blank" class="recommendation-link"><h3>${recData.filename}</h3></a>`;
                if (!recData.link || recData.link === '#') {
                    linkHTML = `<h3>${recData.filename}</h3>`;
                }

                // Use innerHTML for the card structure inside the message div
                recDiv.innerHTML = `
                    <div class="recommendation-card">
                        ${linkHTML}
                        <p class="recommendation-preview">${recData.preview_info || 'No preview available.'}</p>
                    </div>
                `;
                chat.appendChild(recDiv); // Append to main chat area
            }

            // 3. Display Other Sources List (if available) as a new bubble
            if (data.other_sources && data.other_sources.length > 0) {
                 const sources = data.other_sources;
                 const sourcesDiv = document.createElement("div");
                 sourcesDiv.classList.add("message", "bot", "other-sources-container"); // Optional distinct class

                 let sourcesHtml = '<hr style="margin-top: 0; margin-bottom: 10px;"><strong>Other Relevant Sources:</strong><ul>'; // Add HR above list
                 let count = 0;
                 sources.forEach(src => {
                      if (count < 4) {
                           const filename = src.filename || 'Unknown';
                           const link = src.link || '#';
                           const safeFilename = filename.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
                           const safeLink = link.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
                           sourcesHtml += `<li><a href="${safeLink}" target="_blank">${safeFilename.substring(0, 70)}</a></li>`;
                           count++;
                      }
                 });
                 if (sources.length > 4) {
                      sourcesHtml += "<li>... (more sources used)</li>";
                 }
                 sourcesHtml += "</ul>";

                 // Wrap sources in a bubble
                 sourcesDiv.innerHTML = `<div class="bubble">${sourcesHtml}</div>`;
                 chat.appendChild(sourcesDiv); // Append to main chat area
            }

            // Scroll to bottom after potentially adding multiple elements
            chat.scrollTop = chat.scrollHeight;

        } catch (error) {
            // --- Remove temporary message on error too ---
            const tempMsg = chat.querySelector(".temp-thinking");
            if (tempMsg) {
                 tempMsg.remove();
            }
            // --- END Remove ---

            if (error.name === 'AbortError') {
                console.log('Fetch aborted');
                displayMessage("bot", "<p class=\"error\">Request stopped.</p>");
            } else {
                console.error("Fetch error:", error);
                displayMessage("bot", "<p class=\"error\">Sorry, something went wrong communicating with the server.</p>");
            }
        } finally {
            // --- MODIFIED: Show input/send, hide stop, remove class ---
            questionInput.style.display = 'block';
            sendButton.style.display = 'inline-flex'; // Show send button
            stopButton.style.display = 'none'; // Hide stop button
            chatForm.classList.remove('processing'); // Remove class
            // --- END MODIFIED ---
            controller = null; // Reset controller
            questionInput.focus(); // Refocus input field
        }
    });

    // Stop button functionality
    stopButton.addEventListener('click', () => {
        if (controller) {
            controller.abort(); // Abort the ongoing fetch request
            console.log("Stop button clicked, fetch aborted.");
        }
    });

}); // End DOMContentLoaded
