(function () {
  var CSRF = "";
  async function api(path, body) {
    if (!CSRF) { try { CSRF = (await (await fetch("/api/csrf", {cache:"no-store"})).json()).csrfToken || ""; } catch (e) {} }
    var r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json","X-CSRF-Token":CSRF},
                              body: JSON.stringify(body)});
    return r.json();
  }
  // ---- connection ----
  async function loadConn() {
    var d = await api("/api/report-groups", {action:"connection-status"});
    document.getElementById("connStatus").textContent = d.hasConnection
      ? ("Connected as " + d.username + " @ " + d.host + " (" + d.apiMode + ")")
      : "No reporting connection set.";
  }
  async function copyConn() {
    var m = document.getElementById("connMsg"); m.textContent = "Copying and validating…";
    var d = await api("/api/report-groups", {action:"use-current-connection"});
    m.textContent = d.message || (d.ok ? "Done." : "Failed.");
    loadConn(); renderGroups();
  }
  async function validateConn() {
    var m = document.getElementById("connMsg"); m.textContent = "Validating…";
    var d = await api("/api/report-groups", {action:"validate-connection"});
    m.textContent = d.message || (d.ok ? "OK" : "Failed");
  }
  // ---- smtp ----
  async function loadSmtp() {
    var f = document.getElementById("smtpForm");
    try {
      var d = await (await fetch("/api/email-config", {cache:"no-store"})).json();
      var s = d.smtp || {};
      f.host.value=s.host||""; f.port.value=s.port||25; f.security.value=s.security||"none";
      f.username.value=s.username||""; f.from.value=s.from||""; f.opsAlertAddress.value=d.opsAlertAddress||"";
      f.password.placeholder = s.passwordSaved ? "(unchanged — leave blank to keep)" : "";
    } catch (e) {}
  }
  async function saveSmtp(ev) {
    ev.preventDefault(); var f = ev.target, m = document.getElementById("smtpMsg"); m.textContent = "Saving…";
    var d = await api("/api/email-config", {host:f.host.value, port:Number(f.port.value), security:f.security.value,
      username:f.username.value, password:f.password.value, from:f.from.value, opsAlertAddress:f.opsAlertAddress.value});
    m.textContent = d.ok ? "Saved." : "Save failed."; if (d.ok) { f.password.value=""; loadSmtp(); }
  }
  // ---- groups ----
  async function renderGroups() {
    var list = document.getElementById("reportGroupsList");
    var d = await api("/api/report-groups", {action:"list"});
    document.getElementById("reportGroupsConn").textContent = d.hasConnection ? "" : "Set the reporting connection above before enabling groups.";
    var order = (d.groups||[]).map(function(g){return g.id;});
    list.innerHTML = (d.groups||[]).map(function(g){
      var badge = '<span class="health-badge health-'+({healthy:"ok",unhealthy:"bad",never_run:"idle"}[g.health.state]||"idle")+'">'+g.health.state.replace("_"," ")+'</span>';
      return '<div class="report-job" data-id="'+g.id+'">'
        + '<div class="rj-main"><strong>'+g.name+'</strong> · '+g.cadence+' '+g.sendTime+' · '+g.recipients.length+' recipient(s) '+badge+'</div>'
        + '<div class="rj-sub">'+g.sections.join(", ")+' · last: '+(g.health.lastResult||"—")+'</div>'
        + '<label class="check-row"><input type="checkbox" class="rg-toggle" '+(g.enabled?"checked":"")+'> on</label> '
        + '<label class="check-row"><input type="checkbox" class="rg-test"> test</label> '
        + '<button class="rg-send" type="button">Send now</button> <button class="rg-edit" type="button">Edit</button> '
        + '<button class="rg-del" type="button">Delete</button> <button class="rg-up" type="button">↑</button><button class="rg-down" type="button">↓</button></div>';
    }).join("") || "<p>No report groups yet.</p>";
    list.querySelectorAll(".report-job").forEach(function(card){
      var id = card.getAttribute("data-id");
      var g = (d.groups||[]).find(function(x){return x.id===id;});
      card.querySelector(".rg-toggle").addEventListener("change", function(e){ act({action:"toggle", id:id, enabled:e.target.checked}); });
      card.querySelector(".rg-del").addEventListener("click", function(){ act({action:"delete", id:id}); });
      card.querySelector(".rg-send").addEventListener("click", async function(){
        var t = card.querySelector(".rg-test").checked;
        var dd = await api("/api/report-groups", {action:"send", id:id, test:t});
        alert(dd.message || (dd.ok?"Sent":"Failed"));
      });
      card.querySelector(".rg-edit").addEventListener("click", function(){ editGroup(id, g); });
      card.querySelector(".rg-up").addEventListener("click", function(){ move(order, id, -1); });
      card.querySelector(".rg-down").addEventListener("click", function(){ move(order, id, 1); });
    });
  }
  async function act(body){ await api("/api/report-groups", body); renderGroups(); }
  function move(order, id, delta){ var i=order.indexOf(id), j=i+delta; if(i<0||j<0||j>=order.length) return; order.splice(j,0,order.splice(i,1)[0]); act({action:"reorder", order:order}); }
  function editGroup(id, g){
    var f = document.getElementById("reportGroupForm"); f.hidden=false;
    f.id.value=id||""; f.name.value=g?g.name:""; f.recipients.value=g?g.recipients.join(", "):"";
    f.cadence.value=g?g.cadence:"daily"; f.sendTime.value=g?g.sendTime:"08:00"; f.enabled.checked=g?g.enabled:true;
    var sel=g?g.sections:[]; document.querySelectorAll("#reportSectionChecks input").forEach(function(c){ c.checked=sel.indexOf(c.value)>=0; });
    document.getElementById("reportGroupErr").textContent="";
  }
  async function saveGroup(ev){
    ev.preventDefault(); var f=ev.target, err=document.getElementById("reportGroupErr");
    var sections = Array.prototype.slice.call(document.querySelectorAll("#reportSectionChecks input:checked")).map(function(c){return c.value;});
    var d = await api("/api/report-groups", {action: f.id.value?"update":"create", id:f.id.value||undefined,
      name:f.name.value, sections:sections, recipients:f.recipients.value, cadence:f.cadence.value,
      sendTime:f.sendTime.value, enabled:f.enabled.checked});
    if(!d.ok){ err.textContent = "Fix: " + Object.values(d.errors||{message:d.message}).join(" "); return; }
    if(d.warning) err.textContent = d.warning;
    f.hidden = true; renderGroups();
  }
  document.getElementById("connCopyBtn").addEventListener("click", copyConn);
  document.getElementById("connValidateBtn").addEventListener("click", validateConn);
  document.getElementById("smtpForm").addEventListener("submit", saveSmtp);
  document.getElementById("reportGroupAddBtn").addEventListener("click", function(){ editGroup("", null); });
  document.getElementById("reportGroupForm").addEventListener("submit", saveGroup);
  document.getElementById("reportGroupCancel").addEventListener("click", function(){ document.getElementById("reportGroupForm").hidden = true; });
  loadConn(); loadSmtp(); renderGroups();
})();
