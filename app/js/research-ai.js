/**
 * TradeSignal — AI Research Assistant Module
 * Chat interface powered by local Ollama/Llama3 with SSE streaming
 */

class ResearchAI {
  constructor() {
    this._bound = false;
    this._isOpen = false;
    this._messages = [];
    this._streaming = false;
  }

  init() {
    if (this._bound) return;
    this._bound = true;

    // Create floating chat button + panel
    this._injectUI();
    this._bindEvents();
  }

  _injectUI() {
    // Floating button
    const fab = document.createElement('div');
    fab.id = 'ai-fab';
    fab.innerHTML = '🤖';
    fab.style.cssText = `
      position:fixed;bottom:24px;right:24px;width:52px;height:52px;
      border-radius:50%;background:linear-gradient(135deg,#667eea,#764ba2);
      display:flex;align-items:center;justify-content:center;font-size:1.5rem;
      cursor:pointer;z-index:1000;box-shadow:0 4px 20px rgba(102,126,234,0.4);
      transition:transform 0.2s,box-shadow 0.2s;
    `;
    fab.addEventListener('mouseenter', () => fab.style.transform = 'scale(1.1)');
    fab.addEventListener('mouseleave', () => fab.style.transform = 'scale(1)');
    document.body.appendChild(fab);

    // Chat panel
    const panel = document.createElement('div');
    panel.id = 'ai-panel';
    panel.style.cssText = `
      position:fixed;bottom:90px;right:24px;width:380px;max-height:520px;
      background:var(--bg-card,#1a1f2e);border:1px solid rgba(255,255,255,0.08);
      border-radius:16px;display:none;flex-direction:column;z-index:1001;
      box-shadow:0 8px 40px rgba(0,0,0,0.6);overflow:hidden;
    `;
    panel.innerHTML = `
      <div style="padding:14px 16px;background:linear-gradient(135deg,#667eea,#764ba2);display:flex;justify-content:space-between;align-items:center;">
        <div>
          <div style="font-weight:700;font-size:0.95rem;color:#fff;">🤖 AI Research</div>
          <div style="font-size:0.68rem;color:rgba(255,255,255,0.7);">Powered by Ollama · Llama3</div>
        </div>
        <button id="ai-close" style="background:none;border:none;color:rgba(255,255,255,0.8);font-size:1.2rem;cursor:pointer;">✕</button>
      </div>
      <div id="ai-messages" style="flex:1;overflow-y:auto;padding:12px;min-height:250px;max-height:350px;">
        <div style="text-align:center;color:var(--text-muted);font-size:0.78rem;padding:24px 16px;">
          <div style="font-size:2rem;margin-bottom:8px;">🔬</div>
          <p>Ask me about any stock — technical analysis, support/resistance levels, sector outlook, or trading strategies.</p>
          <p style="margin-top:8px;font-size:0.7rem;color:var(--text-muted);">Example: "Analyse RELIANCE for swing trade"</p>
        </div>
      </div>
      <div style="padding:10px 12px;border-top:1px solid rgba(255,255,255,0.06);display:flex;gap:8px;">
        <input type="text" id="ai-symbol" placeholder="Symbol" style="width:80px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);color:var(--text-primary);font-size:0.78rem;" />
        <input type="text" id="ai-query" placeholder="Ask about a stock..." style="flex:1;padding:8px;border-radius:8px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);color:var(--text-primary);font-size:0.78rem;" />
        <button id="ai-send" class="btn btn-sm btn-primary" style="padding:8px 14px;white-space:nowrap;">Send</button>
      </div>
    `;
    document.body.appendChild(panel);
  }

  _bindEvents() {
    document.getElementById('ai-fab')?.addEventListener('click', () => this.toggle());
    document.getElementById('ai-close')?.addEventListener('click', () => this.close());
    document.getElementById('ai-send')?.addEventListener('click', () => this._send());
    document.getElementById('ai-query')?.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
    });
  }

  toggle() {
    this._isOpen ? this.close() : this.open();
  }

  open() {
    const panel = document.getElementById('ai-panel');
    if (panel) { panel.style.display = 'flex'; this._isOpen = true; }
    document.getElementById('ai-query')?.focus();
  }

  close() {
    const panel = document.getElementById('ai-panel');
    if (panel) { panel.style.display = 'none'; this._isOpen = false; }
  }

  async _send() {
    if (this._streaming) return;

    const queryEl = document.getElementById('ai-query');
    const symbolEl = document.getElementById('ai-symbol');
    const query = queryEl?.value?.trim();
    const symbol = symbolEl?.value?.trim().toUpperCase() || '';

    if (!query) return;

    // Add user message
    this._addMessage('user', `${symbol ? `[${symbol}] ` : ''}${query}`);
    queryEl.value = '';

    // Start streaming response
    this._streaming = true;
    const sendBtn = document.getElementById('ai-send');
    if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '...'; }

    const aiMsgEl = this._addMessage('ai', '⏳ Thinking...');

    try {
      const resp = await app.apiFetch('/api/ai/research', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, symbol })
      });

      if (!resp.ok) {
        aiMsgEl.textContent = '⚠️ Failed to connect to AI service.';
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let fullText = '';
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.text) {
              fullText += data.text;
              aiMsgEl.innerHTML = this._formatMarkdown(fullText);
              this._scrollToBottom();
            }
            if (data.done) break;
          } catch (e) { /* skip parse errors */ }
        }
      }

      if (!fullText) aiMsgEl.textContent = '⚠️ No response received. Is Ollama running?';

    } catch (e) {
      aiMsgEl.textContent = `⚠️ ${e.message}`;
    } finally {
      this._streaming = false;
      if (sendBtn) { sendBtn.disabled = false; sendBtn.textContent = 'Send'; }
    }
  }

  _addMessage(role, text) {
    const container = document.getElementById('ai-messages');
    if (!container) return null;

    // Remove welcome message if present
    if (this._messages.length === 0) container.innerHTML = '';

    const msg = document.createElement('div');
    msg.style.cssText = `
      padding:8px 12px;border-radius:12px;margin-bottom:8px;
      font-size:0.8rem;line-height:1.5;word-break:break-word;
      ${role === 'user'
        ? 'background:rgba(102,126,234,0.15);color:#a5b4fc;text-align:right;margin-left:40px;'
        : 'background:rgba(255,255,255,0.05);color:var(--text-secondary);margin-right:20px;'}
    `;
    msg.innerHTML = role === 'user' ? text : this._formatMarkdown(text);
    container.appendChild(msg);
    this._messages.push({ role, text });
    this._scrollToBottom();
    return msg;
  }

  _formatMarkdown(text) {
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/`(.*?)`/g, '<code style="background:rgba(255,255,255,0.08);padding:1px 4px;border-radius:3px;font-size:0.75rem;">$1</code>')
      .replace(/^- (.*)/gm, '• $1')
      .replace(/\n/g, '<br>');
  }

  _scrollToBottom() {
    const container = document.getElementById('ai-messages');
    if (container) container.scrollTop = container.scrollHeight;
  }
}

window.researchAI = new ResearchAI();
