"use strict";
window.LabKnowledge = (() => {
  const $ = id => document.getElementById(id);
  let c, loading = false, importing = false, request = 0, sourceRequest = 0;
  const label = scope => ({default:"通用资料", "b-ospf":"OSPF 实验资料"})[scope] || scope;
  const note = text => { $("knowledge-status").textContent = text; };
  const isOpen = () => $("course-knowledge-dialog").open;
  function sync() {
    if (!c) return;
    $("knowledge-current").textContent = "当前：" + label(c.scope());
    $("apply-knowledge").disabled = loading || importing || c.busy();
    $("save-knowledge").disabled = loading || importing || c.busy();
    $("close-knowledge").disabled = importing;
    $("course-scope").disabled = loading || importing;
    $("search-knowledge").disabled = loading || importing;
  }
  function location(document) {
    return [document.version, document.line_start ? `第 ${document.line_start}～${document.line_end} 行` : "",
      document.source_name].filter(Boolean).join(" · ");
  }
  function passage(document, withContent = false) {
    const row = c.element("article", undefined, "knowledge-document");
    const button = c.element("button", document.title, "knowledge-document-title"); button.type = "button";
    button.addEventListener("click", () => showSource(document));
    row.append(button, c.element("p", location(document), "section-note"));
    if (withContent) row.append(c.element("pre", document.content));
    return row;
  }
  async function load(scope = $("course-scope").value || c.scope()) {
    const mine = ++request; ++sourceRequest;
    loading = true; sync(); note("正在读取资料库…");
    $("knowledge-source").hidden = true; $("knowledge-results").replaceChildren();
    try {
      const [scopes, listing] = await Promise.all([c.api("/api/knowledge/scopes"), c.api("/api/knowledge/documents?" + new URLSearchParams({scope}))]);
      if (mine !== request || !isOpen()) return;
      $("course-scope").replaceChildren();
      for (const item of new Set([...scopes.scopes, scope])) {
        const option = c.element("option", label(item)); option.value = item; $("course-scope").append(option);
      }
      $("course-scope").value = scope;
      $("knowledge-documents").replaceChildren(...listing.documents.map(document => passage(document)));
      $("knowledge-document-count").textContent = `${listing.documents.length} 份资料 · 包含通用说明`;
      note(listing.warnings.join("\n"));
    } catch (error) { if (mine === request && isOpen()) note("资料库加载失败：" + error.message); }
    finally { if (mine === request) { loading = false; sync(); } }
  }
  async function open() {
    if (!isOpen()) $("course-knowledge-dialog").showModal();
    await load(c.scope());
  }
  async function showSource(passage) {
    const mine = ++sourceRequest;
    const section = $("knowledge-source"), content = $("knowledge-source-content");
    section.hidden = false; $("knowledge-source-title").textContent = passage.title;
    $("knowledge-source-note").textContent = "正在读取资料原文…"; content.replaceChildren();
    try {
      const original = await c.api("/api/knowledge/documents/" + encodeURIComponent(passage.id) + "?" + new URLSearchParams({scope:passage.scope || c.scope()}));
      if (mine !== sourceRequest || !isOpen()) return;
      $("knowledge-source-note").textContent = location(passage) + ` · 全文 ${original.line_count} 行`;
      const start = passage.char_start, end = passage.char_end;
      const characters = Array.from(original.content);
      if (Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && characters.slice(start,end).join("") === passage.content) {
        const mark = c.element("mark", passage.content);
        content.append(document.createTextNode(characters.slice(0,start).join("")), mark, document.createTextNode(characters.slice(end).join("")));
        mark.scrollIntoView({block:"nearest"});
      } else content.textContent = original.content;
      section.scrollIntoView({block:"nearest"});
    } catch (error) {
      if (mine !== sourceRequest || !isOpen()) return;
      $("knowledge-source-note").textContent = passage.content ? "原始资料暂时无法读取，下面显示本轮保存的片段。" : "原文读取失败：" + error.message;
      content.textContent = passage.content || "";
    }
  }
  async function search(event) {
    event.preventDefault();
    const query = $("course-query").value.trim();
    if (!query) return note("请填写想查的问题。");
    const mine = ++request;
    ++sourceRequest; $("knowledge-source").hidden = true;
    loading = true; sync(); note("正在检索资料…");
    try {
      const result = await c.api("/api/knowledge/search", {query, scope:$("course-scope").value, limit:3});
      if (mine !== request || !isOpen()) return;
      $("knowledge-results").replaceChildren(...result.results.map(document => passage(document,true)));
      note((result.results.length ? `找到 ${result.results.length} 个片段，点击标题查看原文。` : "没有找到相关片段，可以换个问法或补充资料。") + (result.warnings.length ? "\n" + result.warnings.join("\n") : ""));
    } catch (error) { if (mine === request && isOpen()) note("检索失败：" + error.message); }
    finally { if (mine === request) { loading = false; sync(); } }
  }
  async function save(event) {
    event.preventDefault();
    if (c.busy() || importing) return note("当前诊断结束后即可保存资料。");
    importing = true; sync(); note("正在保存资料…");
    try {
      const scope = $("course-import-scope").value.trim();
      const result = await c.api("/api/knowledge/import", {title:$("course-title").value.trim(), scope,
        content:$("course-content").value, version:$("course-version").value.trim(),
        source_name:$("course-source").value.trim(), reference_url:$("course-url").value.trim()});
      await load(scope);
      note(result.created ? "资料已保存。点击“用于当前排查”后，下一轮诊断可检索这一范围。" : "相同版本和正文的资料已存在，可以直接用于当前排查。");
    } catch (error) { note("保存失败：" + error.message); }
    finally { importing = false; sync(); }
  }
  function close() { if (importing) return; ++request; ++sourceRequest; loading = false; $("course-knowledge-dialog").close(); sync(); }
  function init(context) {
    c = context; sync();
    $("open-knowledge").addEventListener("click",open);
    $("close-knowledge").addEventListener("click",close);
    $("course-knowledge-dialog").addEventListener("cancel", event => { if (importing) event.preventDefault(); else { ++request; ++sourceRequest; loading = false; sync(); } });
    $("course-scope").addEventListener("change",() => load());
    $("course-search-form").addEventListener("submit",search);
    $("course-import-form").addEventListener("submit",save);
    $("apply-knowledge").addEventListener("click",() => {
      if (c.busy()) return note("当前诊断结束后即可切换资料。");
      c.setScope($("course-scope").value); close(); c.message("已选择“" + label(c.scope()) + "”，下一轮诊断生效。");
    });
    $("course-file").addEventListener("change",async event => {
      const file = event.target.files[0]; if (!file) return;
      try {
        if (!/\.(txt|md)$/i.test(file.name) || file.size > 500000) throw new Error("请选择 500KB 以内的 UTF-8 文本或 Markdown 文件。");
        const content = new TextDecoder("utf-8", {fatal:true}).decode(await file.arrayBuffer());
        if (content.length > 150000) throw new Error("资料正文最多 150000 字，请按章节拆分。");
        $("course-content").value = content; $("course-title").value = file.name.replace(/\.[^.]+$/,"").slice(0,120);
        $("course-source").value = file.name.slice(0,200); note("文件已读入，请确认资料范围、版本和来源后保存。");
      } catch (error) { note(error.message); }
      finally { event.target.value = ""; }
    });
  }
  async function showPassage(document) { await open(); if (isOpen()) await showSource(document); }
  return {init,sync,showPassage};
})();
