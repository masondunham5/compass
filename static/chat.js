/* Chat with Compass: type or talk, review the proposed changes, confirm or undo.
   POST /api/chat returns {reply, proposals}; nothing changes until Confirm (POST /api/chat/apply/{id}).
   Voice uses the browser's speech recognition; replies to spoken messages are read aloud.
   Global names are prefixed "chat" (all scripts share one scope). */

const chatState = { messages: [], busy: false, listening: false, recognizer: null, spoke: false };
const ChatRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

function chatOpen() {
  const dialog = $("#chat-sheet");
  if (!chatState.messages.length) chatRender();
  openDialog(dialog);
  setTimeout(() => $("#chat-input").focus(), 60);
}

function chatRender() {
  const log = $("#chat-log");
  if (!chatState.messages.length) {
    log.innerHTML = `<div class="chat-hint">Ask me to change your schedule. Nothing changes until you confirm.<br>
      ${["Swap chest and back this week and next", "Push training back 2 days", "Add to-do: call insurance, 20 min, due friday", "Move my haircut to Thursday at 4"]
        .map(text => `<button type="button" class="chip" data-chat-example="${esc(text)}">${esc(text)}</button>`).join("")}</div>`;
    log.querySelectorAll("[data-chat-example]").forEach(button => button.onclick = () => chatSend(button.dataset.chatExample));
  }
}

function chatBubble(role, text, extra = "") {
  const log = $("#chat-log");
  log.querySelector(".chat-hint")?.remove();
  const bubble = document.createElement("div");
  bubble.className = `chat-msg ${role}${extra ? ` ${extra}` : ""}`;
  bubble.textContent = text;
  log.append(bubble);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

function chatProposal(card) {
  const box = document.createElement("div");
  box.className = "proposal";
  const details = card.details.filter(Boolean);
  box.innerHTML = `<b>${esc(card.summary)}</b>${details.length ? `<ul>${details.map(d => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}
    ${card.warning ? `<p class="note error">${esc(card.warning)}</p>` : ""}
    <div class="sheet-actions"><button type="button" class="btn btn-ghost small" data-act="dismiss">Not now</button><button type="button" class="btn btn-primary small" data-act="confirm">Confirm</button></div>`;
  box.querySelector('[data-act="dismiss"]').onclick = () => { box.querySelector(".sheet-actions").outerHTML = `<p class="faint">Skipped.</p>`; };
  box.querySelector('[data-act="confirm"]').onclick = event => chatApply(card, box, event.currentTarget);
  $("#chat-log").append(box);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
}

async function chatApply(card, box, button) {
  button.disabled = true;
  try {
    const result = await api(`/api/chat/apply/${encodeURIComponent(card.id)}`, { method: "POST" });
    box.querySelector(".sheet-actions").outerHTML = `<p class="done-line">✓ ${esc(result.summary)}</p>${result.undo ? `<div class="sheet-actions"><button type="button" class="btn btn-ghost small" data-act="undo">Undo</button></div>` : ""}`;
    const undo = box.querySelector('[data-act="undo"]');
    if (undo) undo.onclick = async () => {
      undo.disabled = true;
      try {
        const back = await api(`/api/chat/apply/${encodeURIComponent(result.undo.id)}`, { method: "POST" });
        undo.parentElement.outerHTML = `<p class="faint">↩ ${esc(back.summary)}</p>`;
        await chatRefresh();
      } catch (error) { alert(error.message); undo.disabled = false; }
    };
    await chatRefresh();
  } catch (error) { alert(error.message); button.disabled = false; }
}

async function chatRefresh() {
  try {
    if (typeof load === "function") await load();
    if (typeof refreshPlanViews === "function") await refreshPlanViews();
  } catch { /* the next view render catches up */ }
}

async function chatSend(text, spoken = false) {
  text = String(text || "").trim();
  if (!text || chatState.busy) return;
  chatState.busy = true; chatState.spoke = spoken;
  $("#chat-input").value = "";
  chatBubble("user", text);
  chatState.messages.push({ role: "user", content: text });
  const thinking = chatBubble("assistant", "Thinking…", "thinking");
  try {
    const result = await api("/api/chat", { method: "POST", body: JSON.stringify({ messages: chatState.messages.slice(-12) }) });
    thinking.remove();
    const bubble = chatBubble("assistant", result.reply);
    if (result.source) bubble.insertAdjacentHTML("beforeend", `<small>${esc(result.source === "built-in" ? "Built-in command" : result.source)}</small>`);
    chatState.messages.push({ role: "assistant", content: result.reply + (result.proposals.length ? ` [Proposed: ${result.proposals.map(p => p.summary).join("; ")}]` : "") });
    result.proposals.forEach(chatProposal);
    if (spoken && "speechSynthesis" in window) {
      speechSynthesis.cancel();
      speechSynthesis.speak(new SpeechSynthesisUtterance(result.reply));
    }
  } catch (error) {
    thinking.remove();
    chatBubble("assistant", error.message);
    chatState.messages.pop();  // let them retry the same message
  } finally { chatState.busy = false; }
}

function chatListen() {
  if (!ChatRecognition) return;
  if (chatState.listening) { chatState.recognizer?.stop(); return; }
  const recognizer = new ChatRecognition();
  recognizer.lang = navigator.language || "en-US";
  recognizer.interimResults = true;
  recognizer.continuous = false;
  let finalText = "";
  recognizer.onresult = event => {
    let interim = "";
    for (const result of event.results) { if (result.isFinal) finalText = result[0].transcript; else interim += result[0].transcript; }
    $("#chat-input").value = finalText || interim;
  };
  recognizer.onerror = event => { if (event.error === "not-allowed") alert("Allow microphone access for Compass in your browser to talk to it."); };
  recognizer.onend = () => {
    chatState.listening = false; $("#chat-mic").classList.remove("listening");
    if (finalText) chatSend(finalText, true);
  };
  chatState.recognizer = recognizer; chatState.listening = true;
  $("#chat-mic").classList.add("listening");
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  recognizer.start();
}

/* Settings → AI assistant */
async function renderChatSettings(status = null) {
  const box = $("#chat-settings-body");
  if (!box) return;
  try { status = status || await api("/api/chat/status"); } catch { return; }
  if (status.connected) {
    box.innerHTML = `<p><b>Connected</b> to ${esc(status.provider)} · model ${esc(status.model)}.</p><button class="btn btn-ghost" type="button" id="chat-key-remove">Remove key</button>`;
    $("#chat-key-remove").onclick = async () => { if (confirm("Remove the AI key from this computer? Built-in commands keep working.")) renderChatSettings(await api("/api/chat/key", { method: "DELETE" })); };
    return;
  }
  box.innerHTML = `<form id="chat-key-form" class="inline-form"><input id="chat-key" type="password" placeholder="API key (gsk_…)" autocomplete="off" spellcheck="false" required><button class="btn btn-primary" type="submit">Save</button></form>
    <p class="faint">Stored encrypted on this computer and only sent to the provider.</p><p class="note error" id="chat-key-error" hidden></p>`;
  $("#chat-key-form").onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter; if (button) button.disabled = true;
    try { renderChatSettings(await api("/api/chat/key", { method: "POST", body: JSON.stringify({ key: $("#chat-key").value.trim() }) })); }
    catch (error) { $("#chat-key-error").textContent = error.message; $("#chat-key-error").hidden = false; if (button) button.disabled = false; }
  };
}

$("#chat-fab").onclick = chatOpen;
$("#chat-sheet .sheet-close").onclick = () => $("#chat-sheet").close();
$("#chat-form").onsubmit = event => { event.preventDefault(); chatSend($("#chat-input").value); };
if (ChatRecognition) { $("#chat-mic").hidden = false; $("#chat-mic").onclick = chatListen; }
renderChatSettings();
