/* Settings → Places & travel: your places (address + keywords that match event locations),
   travel times between them (estimated from OpenStreetMap or typed in), how you get around,
   and where you lift. Global names are prefixed "places" (all scripts share one scope). */

const placesState = { data: null, editing: null };

async function placesLoad() {
  try { placesRender(await api("/api/assistant/places")); } catch { /* settings stays empty */ }
}

function placesKeyword(location) {
  // "Bessey Hall 108" -> "Bessey Hall"; "BCC N17" -> "BCC": drop room numbers at the end.
  const words = location.split(/\s+/);
  while (words.length > 1 && /\d/.test(words[words.length - 1])) words.pop();
  return words.join(" ").replace(/[(,].*$/, "").trim();
}

function placesRender(data) {
  placesState.data = data;
  const box = $("#places-body");
  if (!box) return;
  const kinds = data.kinds;
  const options = (selected, includeHome = false) => (includeHome ? `<option value="">At home (no travel)</option>` : "")
    + data.places.map(p => `<option value="${esc(p.id)}" ${p.id === selected ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  const editing = data.places.find(p => p.id === placesState.editing) || null;
  const list = data.places.length
    ? data.places.map(p => `<div class="row-item"><div class="grow"><b>${esc(p.name)} <span class="muted">· ${esc(kinds[p.kind] || p.kind)}${p.kind === "home" && p.home_for !== "always" ? ` ${esc(data.home_for[p.home_for].toLowerCase())}` : ""}</span></b>
        <small>${esc(p.address || "No address yet")}${p.keywords ? ` · matches: ${esc(p.keywords)}` : ""}${p.extra && !(data.parking[p.id] || []).length ? ` · +${p.extra} min to park/walk` : ""}</small>
        ${placesParking(p, data)}</div>
        <div class="record-actions"><button type="button" data-place-edit="${esc(p.id)}">Edit</button><button type="button" data-place-remove="${esc(p.id)}">Remove</button></div></div>`).join("")
    : `<p class="muted">Start with your home (your apartment during school terms, and your family home during breaks), then add Campus, Work and anywhere else you go.</p>`;
  const homes = data.places.filter(p => p.kind === "home" && p.id !== placesState.editing).map(p => p.home_for);
  const homeDefault = editing?.home_for || (homes.includes("school") ? "break" : homes.includes("break") ? "school" : "school");
  const termNote = data.has_terms
    ? `<p class="faint">Today counts as a ${data.today_term === "break" ? "break" : "school term"}. Days outside your school terms (More → Work schedule) count as breaks.</p>`
    : `<p class="note">No school terms set yet, so every day counts as a school term. Add your semester dates in More → Work schedule; days outside them count as breaks, and your break home takes over.</p>`;
  const travel = data.travel.length
    ? `<div class="travel-grid">${data.travel.map(t => `<label>${esc(t.a_name)} ↔ ${esc(t.b_name)}<span>${t.manual ? "your number" : t.minutes == null ? "not known yet" : "estimated"}</span>
        <input type="number" min="0" max="240" step="5" inputmode="numeric" value="${t.minutes ?? ""}" placeholder="min" data-travel-a="${esc(t.a)}" data-travel-b="${esc(t.b)}"></label>`).join("")}</div>`
    : "";
  const unmatched = data.unmatched.length && data.places.length
    ? `<details class="unmatched"><summary>${data.unmatched.length} event location${data.unmatched.length === 1 ? "" : "s"} not matched to a place</summary>
        ${data.unmatched.map(u => `<div class="row-item"><div class="grow"><b>${esc(u.location)}</b><small>${u.count} event${u.count === 1 ? "" : "s"} in the next month</small></div>
          <select data-unmatched="${esc(u.location)}"><option value="">Add to…</option>${options("")}</select></div>`).join("")}</details>`
    : "";
  box.innerHTML = `${termNote}<div class="stack">${list}</div>
    <form id="place-form" class="stack place-form">
      <p class="kicker">${editing ? `Edit ${esc(editing.name)}` : "Add a place"}</p>
      <div class="form-grid"><label>Name<input id="place-name" maxlength="60" required value="${esc(editing?.name || "")}" placeholder="Campus"></label>
        <label>Kind<select id="place-kind">${Object.entries(kinds).map(([k, label]) => `<option value="${k}" ${k === (editing?.kind || (data.places.length ? "campus" : "home")) ? "selected" : ""}>${label}</option>`).join("")}</select></label></div>
      <label id="place-home-for-label" ${(editing?.kind || (data.places.length ? "campus" : "home")) === "home" ? "" : "hidden"}>Home during
        <select id="place-home-for">${Object.entries(data.home_for).map(([k, label]) => `<option value="${k}" ${k === homeDefault ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <label>Address<input id="place-address" maxlength="200" value="${esc(editing?.address || "")}" placeholder="Street, city, state"></label>
      <label>Matches locations containing <span>comma-separated, e.g. Science Hall, Library, Student Union</span><input id="place-keywords" maxlength="500" value="${esc(editing?.keywords || "")}"></label>
      <label>Extra minutes on arrival <span>parking, walking to the building</span><input id="place-extra" type="number" min="0" max="60" step="5" value="${editing?.extra ?? 0}"></label>
      <p class="note error" id="place-error" hidden></p>
      <div class="sheet-actions">${editing ? `<button class="btn btn-ghost" type="button" id="place-cancel">Cancel</button>` : ""}<button class="btn btn-primary" type="submit">${editing ? "Save" : "Add place"}</button></div>
    </form>
    ${data.places.length > 1 ? `<div class="stack"><p class="kicker">Travel times</p>
      <div class="form-grid"><label>Getting around<select id="travel-mode">${[["drive", "Driving"], ["walk", "Walking"], ["bike", "Biking"]].map(([v, l]) => `<option value="${v}" ${v === data.mode ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      </div>
      <div class="form-grid"><label>Lifting during school terms<select id="lift-place-school">${options(data.lift_place_school, true)}</select></label>
        <label>Lifting during breaks<select id="lift-place-break">${options(data.lift_place_break, true)}</select></label></div>
      ${travel}
      <button class="btn btn-ghost" type="button" id="travel-estimate">Estimate from the map</button>
      <p class="faint">Estimates use OpenStreetMap; only your places' addresses are sent. Numbers you type are kept.</p>
      <p class="note" id="travel-status" hidden></p></div>` : ""}
    ${unmatched}`;

  $("#place-form").onsubmit = async event => {
    event.preventDefault();
    const body = { name: $("#place-name").value.trim(), kind: $("#place-kind").value, address: $("#place-address").value.trim(),
      keywords: $("#place-keywords").value.trim(), extra: Number($("#place-extra").value || 0), home_for: $("#place-home-for").value };
    try {
      const url = editing ? `/api/assistant/places/${encodeURIComponent(editing.id)}` : "/api/assistant/places";
      placesState.editing = null;
      placesRender(await api(url, { method: editing ? "PATCH" : "POST", body: JSON.stringify(body) }));
    } catch (error) { $("#place-error").textContent = error.message; $("#place-error").hidden = false; }
  };
  const cancel = $("#place-cancel");
  if (cancel) cancel.onclick = () => { placesState.editing = null; placesRender(placesState.data); };
  box.querySelectorAll("[data-place-edit]").forEach(button => button.onclick = () => { placesState.editing = button.dataset.placeEdit; placesRender(placesState.data); $("#place-name").focus(); });
  const parkingCall = async (placeId, body) => placesRender(await api(`/api/assistant/places/${encodeURIComponent(placeId)}/parking`, { method: "PUT", body: JSON.stringify(body) }));
  box.querySelectorAll("[data-parking-add]").forEach(select => select.onchange = () => select.value && parkingCall(select.dataset.parkingAdd, { parking_id: select.value }).catch(error => alert(error.message)));
  box.querySelectorAll("[data-parking-walk]").forEach(input => input.onchange = () => {
    const [placeId, parkingId] = input.dataset.parkingWalk.split("|");
    parkingCall(placeId, { parking_id: parkingId, walk: input.value === "" ? null : Number(input.value) }).catch(error => alert(error.message));
  });
  box.querySelectorAll("[data-parking-usual]").forEach(input => input.onchange = () => {
    const [placeId, parkingId] = input.dataset.parkingUsual.split("|");
    parkingCall(placeId, { parking_id: parkingId, preferred: input.checked }).catch(error => alert(error.message));
  });
  box.querySelectorAll("[data-parking-remove]").forEach(button => button.onclick = async () => {
    const [placeId, parkingId] = button.dataset.parkingRemove.split("|");
    placesRender(await api(`/api/assistant/places/${encodeURIComponent(placeId)}/parking/${encodeURIComponent(parkingId)}`, { method: "DELETE" }));
  });
  box.querySelectorAll("[data-place-remove]").forEach(button => button.onclick = async () => {
    const place = data.places.find(p => p.id === button.dataset.placeRemove);
    if (confirm(`Remove ${place.name}? Its travel times go too.`)) placesRender(await api(`/api/assistant/places/${encodeURIComponent(place.id)}`, { method: "DELETE" }));
  });
  box.querySelectorAll("[data-travel-a]").forEach(input => input.onchange = async () => {
    const minutes = input.value === "" ? null : Number(input.value);
    placesRender(await api("/api/assistant/places/travel", { method: "PUT", body: JSON.stringify({ a: input.dataset.travelA, b: input.dataset.travelB, minutes }) }));
  });
  const travelMode = $("#travel-mode");
  if (travelMode) travelMode.onchange = async () => placesRender(await api("/api/assistant/places/settings", { method: "PUT", body: JSON.stringify({ mode: travelMode.value }) }));
  $("#place-kind").onchange = event => { $("#place-home-for-label").hidden = event.target.value !== "home"; };
  for (const term of ["school", "break"]) {
    const lift = $(`#lift-place-${term}`);
    if (lift) lift.onchange = async () => placesRender(await api("/api/assistant/places/settings", { method: "PUT", body: JSON.stringify({ [`lift_place_${term}`]: lift.value }) }));
  }
  const estimate = $("#travel-estimate");
  if (estimate) estimate.onclick = async () => {
    estimate.disabled = true; estimate.textContent = "Estimating…";
    try {
      const result = await api("/api/assistant/places/estimate", { method: "POST" });
      placesRender(result);
      const status = $("#travel-status");
      status.textContent = `Estimated ${result.result.estimated} trip${result.result.estimated === 1 ? "" : "s"}.${result.result.problems.length ? ` ${result.result.problems.join(" ")}` : ""}`;
      status.hidden = false;
    } catch (error) { alert(error.message); estimate.disabled = false; estimate.textContent = "Estimate from the map"; }
  };
  box.querySelectorAll("[data-unmatched]").forEach(select => select.onchange = async () => {
    const place = data.places.find(p => p.id === select.value);
    if (!place) return;
    const keyword = placesKeyword(select.dataset.unmatched);
    const keywords = [place.keywords, keyword].filter(Boolean).join(", ");
    placesRender(await api(`/api/assistant/places/${encodeURIComponent(place.id)}`, { method: "PATCH", body: JSON.stringify({ keywords }) }));
  });
}

/* "Park at" for a place: its parking spots, the walk from each, and which one you usually use. */
function placesParking(place, data) {
  if (place.kind === "parking" || place.kind === "home") return "";
  const spots = data.places.filter(p => p.kind === "parking");
  if (!spots.length) return "";
  const mine = data.parking[place.id] || [];
  const free = spots.filter(spot => !mine.some(m => m.parking_id === spot.id));
  return `<div class="parking-list">${mine.map(m => `<div class="parking-row"><span>Park at <b>${esc(m.name)}</b></span>
      <label class="inline-num"><input type="number" min="0" max="60" inputmode="numeric" value="${m.walk ?? ""}" placeholder="?" data-parking-walk="${esc(place.id)}|${esc(m.parking_id)}"> min walk${m.walk == null ? " (estimate from the map)" : m.manual ? "" : " (estimated)"}</label>
      <label class="inline-check"><input type="radio" name="usual-${esc(place.id)}" ${m.preferred ? "checked" : ""} data-parking-usual="${esc(place.id)}|${esc(m.parking_id)}"> usually</label>
      <button type="button" class="link-btn" data-parking-remove="${esc(place.id)}|${esc(m.parking_id)}">remove</button></div>`).join("")}
    ${free.length ? `<select class="parking-add" data-parking-add="${esc(place.id)}"><option value="">+ Where you park…</option>${free.map(spot => `<option value="${esc(spot.id)}">${esc(spot.name)}</option>`).join("")}</select>` : ""}</div>`;
}

placesLoad();
