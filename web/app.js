"use strict";

const $ = (id) => document.getElementById(id);
let csrfToken = "";
let busy = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function safeLink(url, text) {
  const link = el("a", null, text);
  if (typeof url === "string" && url.startsWith("https://")) {
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  return link;
}

function formatTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function showSignedOut(message, isError = true) {
  $("loading").hidden = true;
  $("signed-in").hidden = true;
  $("account").hidden = true;
  $("signed-out").hidden = false;
  const note = $("signin-message");
  note.textContent = message || "";
  note.className = isError ? "error" : "notice";
  note.hidden = !message;
  csrfToken = "";
}

function showSignedIn(me) {
  $("loading").hidden = true;
  $("signed-out").hidden = true;
  $("signed-in").hidden = false;
  $("account").hidden = false;
  $("account-name").textContent = me.user.name || me.user.upn || "";
  $("id-user").textContent = `${me.user.name || ""} <${me.user.upn || ""}>`;
  $("id-auth").textContent = (me.auth_methods || []).join(", ") + (me.mfa ? " (MFA satisfied)" : "");
  $("id-agent").textContent = `${me.agent.name} (appId ${me.agent.app_id}), acting on your behalf (OBO)`;
  $("id-scopes").textContent = me.agent.scopes || "";
  $("id-expires").textContent = new Date(me.expires).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

async function init() {
  const params = new URLSearchParams(window.location.search);
  const signinError = params.get("signin_error");
  if (window.location.search) window.history.replaceState(null, "", "/");
  try {
    const res = await fetch("/api/me", { credentials: "same-origin" });
    if (res.status === 401) {
      showSignedOut(signinError);
      return;
    }
    const me = await res.json();
    csrfToken = me.csrf;
    showSignedIn(me);
  } catch (err) {
    showSignedOut(`Can't reach the agent server: ${err.message}`);
  }
}

function setBusy(value) {
  busy = value;
  $("get-news").disabled = value;
  $("chat-send").disabled = value;
}

async function streamPost(url, body, onEvent) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) {
    const data = await res.json().catch(() => ({}));
    showSignedOut(data.error || "Please sign in again.");
    return;
  }
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

function renderStories(stories) {
  const container = $("news");
  container.replaceChildren();
  stories.forEach((story, index) => {
    const card = el("article", "story");
    card.append(el("div", "rank", String(index + 1)));
    const body = el("div", "story-body");
    const title = el("h3");
    title.append(safeLink(story.article.link, story.headline));
    body.append(title);
    body.append(el("div", "meta", [story.article.source, formatTime(story.article.published)].filter(Boolean).join(" · ")));
    body.append(el("p", null, story.summary));
    if (story.why_it_matters) {
      const why = el("p", "why");
      why.append(el("strong", null, "Why it matters: "), document.createTextNode(story.why_it_matters));
      body.append(why);
    }
    body.append(el("p", "original", `Original headline: “${story.article.title}”`));
    card.append(body);
    container.append(card);
  });
}

async function getNews() {
  if (busy) return;
  setBusy(true);
  const button = $("get-news");
  button.textContent = "The agent is checking the news…";
  const activity = $("news-activity");
  activity.replaceChildren();
  activity.hidden = false;
  $("news").replaceChildren();
  $("news-error").hidden = true;
  try {
    await streamPost("/api/news", {}, (event) => {
      if (event.type === "log") activity.append(el("li", null, event.message));
      else if (event.type === "news") renderStories(event.stories);
      else if (event.type === "auth_error") showSignedOut(event.message);
      else if (event.type === "error") {
        $("news-error").textContent = event.message;
        $("news-error").hidden = false;
      }
    });
  } catch (err) {
    $("news-error").textContent = err.message;
    $("news-error").hidden = false;
  } finally {
    button.textContent = "Get today's top 5";
    setBusy(false);
  }
}

function renderAnswer(reply, answer) {
  reply.append(el("p", "answer", answer.text));
  if (answer.sources && answer.sources.length) {
    const list = el("ol", "sources");
    answer.sources.forEach((source) => {
      const item = el("li");
      item.append(safeLink(source.link, source.title), document.createTextNode(` (${source.source})`));
      list.append(item);
    });
    reply.append(list);
  }
}

async function sendQuestion(event) {
  event.preventDefault();
  const input = $("chat-input");
  const question = input.value.trim();
  if (!question || busy) return;
  input.value = "";
  const hint = $("chat-hint");
  if (hint) hint.remove();
  const messages = $("messages");
  messages.append(el("div", "bubble user", question));
  const reply = el("div", "bubble agent");
  const steps = el("ol", "activity small");
  reply.append(steps);
  messages.append(reply);
  messages.scrollTop = messages.scrollHeight;
  setBusy(true);
  try {
    await streamPost("/api/chat", { message: question }, (evt) => {
      if (evt.type === "log") steps.append(el("li", null, evt.message));
      else if (evt.type === "answer") renderAnswer(reply, evt);
      else if (evt.type === "auth_error") showSignedOut(evt.message);
      else if (evt.type === "error") reply.append(el("p", "error", evt.message));
      messages.scrollTop = messages.scrollHeight;
    });
  } catch (err) {
    reply.append(el("p", "error", err.message));
  } finally {
    setBusy(false);
    input.focus();
  }
}

async function signOut() {
  await fetch("/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken },
  }).catch(() => {});
  showSignedOut("You're signed out.", false);
}

$("get-news").addEventListener("click", getNews);
$("chat-form").addEventListener("submit", sendQuestion);
$("sign-out").addEventListener("click", signOut);
init();
