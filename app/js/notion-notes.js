/**
 * TradeSignal — Notion Notes Workspace Controller
 * Premium module for creating, editing, and syncing trading notes to Notion.
 * Includes interactive, Notion-style WYSIWYG visual grid tables.
 */

class NotionNotesWorkspace {
  constructor() {
    this.notes = [];
    this.activeNote = null;
    this.notionConfig = { has_key: false, parent_id: '', parent_type: 'database' };
    this.isEditing = false;
    this.activeTableData = null; // Visual Notion Table block data
  }

  init() {
    console.log('Notion Notes workspace module initialized.');
    this.populateSymbolDropdowns();
    this.loadNotionConfig();
    this.loadNotes();
    this.bindEvents();
  }

  populateSymbolDropdowns() {
    const filterSelect = document.getElementById('note-filter-symbol');
    const activeSelect = document.getElementById('active-note-symbol');
    if (!filterSelect || !activeSelect) return;

    filterSelect.innerHTML = '<option value="">All Stocks</option>';
    activeSelect.innerHTML = '<option value="">None (General)</option>';

    let stocks = [];
    if (window.equityScreener && typeof window.equityScreener.getFNOUniverseSync === 'function') {
      stocks = window.equityScreener.getFNOUniverseSync();
    } else {
      stocks = [
        { symbol: 'NIFTY' }, { symbol: 'BANKNIFTY' }, { symbol: 'FINNIFTY' },
        { symbol: 'RELIANCE' }, { symbol: 'TCS' }, { symbol: 'INFY' },
        { symbol: 'HDFCBANK' }, { symbol: 'ICICIBANK' }, { symbol: 'SBIN' }
      ];
    }

    stocks.forEach(s => {
      const opt1 = document.createElement('option');
      opt1.value = s.symbol;
      opt1.textContent = s.symbol;
      filterSelect.appendChild(opt1);

      const opt2 = document.createElement('option');
      opt2.value = s.symbol;
      opt2.textContent = s.symbol;
      activeSelect.appendChild(opt2);
    });
  }

  bindEvents() {
    // Config panel toggle
    const toggleConfigBtn = document.getElementById('btn-toggle-notion-config');
    const configPanel = document.getElementById('notion-config-panel');
    if (toggleConfigBtn && configPanel) {
      toggleConfigBtn.addEventListener('click', () => {
        const isHidden = configPanel.style.display === 'none';
        configPanel.style.display = isHidden ? 'block' : 'none';
        toggleConfigBtn.classList.toggle('active', isHidden);
      });
    }

    // Save configuration
    const saveConfigBtn = document.getElementById('btn-save-notion-config');
    if (saveConfigBtn) {
      saveConfigBtn.addEventListener('click', () => this.saveNotionConfig());
    }

    // Test connection
    const testConfigBtn = document.getElementById('btn-test-notion-connection');
    if (testConfigBtn) {
      testConfigBtn.addEventListener('click', () => this.testNotionConnection());
    }

    // New Note
    const newNoteBtn = document.getElementById('btn-new-note');
    if (newNoteBtn) {
      newNoteBtn.addEventListener('click', () => this.createNewNote());
    }

    // Save Note
    const saveNoteBtn = document.getElementById('btn-save-note');
    if (saveNoteBtn) {
      saveNoteBtn.addEventListener('click', () => this.saveActiveNote());
    }

    // Delete Note
    const deleteNoteBtn = document.getElementById('btn-delete-note');
    if (deleteNoteBtn) {
      deleteNoteBtn.addEventListener('click', () => this.deleteActiveNote());
    }

    // Sync to Notion
    const syncNoteBtn = document.getElementById('btn-sync-note');
    if (syncNoteBtn) {
      syncNoteBtn.addEventListener('click', () => this.syncActiveNoteToNotion());
    }

    // Search and filters
    const searchInput = document.getElementById('note-search');
    if (searchInput) {
      searchInput.addEventListener('input', () => this.renderNotesList());
    }

    const filterSelect = document.getElementById('note-filter-symbol');
    if (filterSelect) {
      filterSelect.addEventListener('change', () => this.renderNotesList());
    }

    // Editor vs Preview Tabs
    const tabBtns = document.querySelectorAll('#editor-tabs .tab-btn');
    const textarea = document.getElementById('active-note-content');
    const previewDiv = document.getElementById('active-note-preview');

    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        tabBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const tab = btn.dataset.tab;

        if (tab === 'preview') {
          if (textarea && previewDiv) {
            let combinedText = textarea.value;
            if (this.activeTableData) {
              const tableMd = this.visualTableToMarkdown(this.activeTableData);
              if (tableMd) {
                combinedText = combinedText.trim() + '\n' + tableMd;
              }
            }
            previewDiv.innerHTML = this.parseMarkdown(combinedText);
            textarea.style.display = 'none';
            previewDiv.style.display = 'block';

            // Hide the visual editor in preview mode so it doesn't duplicate
            const tableContainer = document.getElementById('visual-table-container');
            if (tableContainer) tableContainer.style.display = 'none';
          }
        } else {
          if (textarea && previewDiv) {
            textarea.style.display = 'block';
            previewDiv.style.display = 'none';

            // Restore visual table container inside edit mode
            if (this.activeTableData) {
              const tableContainer = document.getElementById('visual-table-container');
              if (tableContainer) tableContainer.style.display = 'flex';
            }
          }
        }
      });
    });

    // Auto-save on typing (debounce)
    let autoSaveTimeout = null;
    const activeTitle = document.getElementById('active-note-title');
    const activeContent = document.getElementById('active-note-content');
    const activeSymbol = document.getElementById('active-note-symbol');
    const activeSentiment = document.getElementById('active-note-sentiment');

    const triggerAutoSave = () => {
      if (!this.activeNote) return;
      
      const saveIndicator = document.getElementById('note-save-indicator');
      if (saveIndicator) {
        saveIndicator.style.display = 'inline';
        saveIndicator.textContent = 'Typing...';
      }

      if (autoSaveTimeout) clearTimeout(autoSaveTimeout);
      autoSaveTimeout = setTimeout(() => {
        this.saveActiveNote(true); // silent auto-save
      }, 1500);
    };

    if (activeTitle) activeTitle.addEventListener('input', triggerAutoSave);
    if (activeContent) activeContent.addEventListener('input', triggerAutoSave);
    if (activeSymbol) activeSymbol.addEventListener('change', triggerAutoSave);
    if (activeSentiment) activeSentiment.addEventListener('change', triggerAutoSave);

    // Clipboard image pasting listener
    if (activeContent) {
      activeContent.addEventListener('paste', async (event) => {
        const clipboardItems = (event.clipboardData || window.clipboardData).items;
        for (let item of clipboardItems) {
          if (item.type.indexOf('image') === 0) {
            // Prevent default pasting behavior
            event.preventDefault();

            const file = item.getAsFile();
            const formData = new FormData();
            formData.append('image', file);

            const saveIndicator = document.getElementById('note-save-indicator');
            if (saveIndicator) {
              saveIndicator.style.display = 'inline';
              saveIndicator.textContent = 'Uploading image crop...';
            }

            try {
              const response = await fetch('/api/notes/upload-image', {
                method: 'POST',
                body: formData
              });
              const result = await response.json();
              if (result.success) {
                const cursorPos = activeContent.selectionStart;
                const text = activeContent.value;
                const imageMarkdown = `\n![Pasted Snapshot](${result.url})\n`;
                activeContent.value = text.slice(0, cursorPos) + imageMarkdown + text.slice(cursorPos);
                
                // Trigger auto save
                triggerAutoSave();
                
                if (saveIndicator) {
                  saveIndicator.textContent = 'Image pasted!';
                  setTimeout(() => {
                    if (saveIndicator.textContent === 'Image pasted!') saveIndicator.style.display = 'none';
                  }, 2000);
                }
              } else {
                alert('Upload failed: ' + result.error);
              }
            } catch (err) {
              alert('Image paste upload failed: ' + err);
            }
          }
        }
      });
    }

    // Bind Visual Table buttons
    const btnAddTable = document.getElementById('btn-visual-add-table');
    const btnAddRow = document.getElementById('btn-visual-add-row');
    const btnAddCol = document.getElementById('btn-visual-add-col');
    const btnRemoveTable = document.getElementById('btn-visual-remove');

    if (btnAddTable) btnAddTable.addEventListener('click', () => this.createVisualTableBlock());
    if (btnAddRow) btnAddRow.addEventListener('click', () => this.addVisualRow());
    if (btnAddCol) btnAddCol.addEventListener('click', () => this.addVisualCol());
    if (btnRemoveTable) btnRemoveTable.addEventListener('click', () => this.removeVisualTable());
  }

  loadNotionConfig() {
    fetch('/api/notion/config')
      .then(res => res.json())
      .then(config => {
        this.notionConfig = config;
        
        // Populate inputs
        const tokenInput = document.getElementById('notion-token');
        const parentInput = document.getElementById('notion-parent-id');
        const typeSelect = document.getElementById('notion-parent-type');

        if (tokenInput && config.has_key) tokenInput.value = '********';
        if (parentInput) parentInput.value = config.parent_id || '';
        if (typeSelect) typeSelect.value = config.parent_type || 'database';

        this.updateNotionStatusBadge();
      })
      .catch(err => console.error('Failed to load Notion config:', err));
  }

  saveNotionConfig() {
    const api_key = document.getElementById('notion-token')?.value || '';
    const parent_id = document.getElementById('notion-parent-id')?.value || '';
    const parent_type = document.getElementById('notion-parent-type')?.value || 'database';

    const saveBtn = document.getElementById('btn-save-notion-config');
    const origText = saveBtn ? saveBtn.textContent : 'Save';
    if (saveBtn) saveBtn.textContent = 'Saving...';

    fetch('/api/notion/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key, parent_id, parent_type })
    })
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          alert('Notion settings saved successfully!');
          this.loadNotionConfig();
          const configPanel = document.getElementById('notion-config-panel');
          if (configPanel) configPanel.style.display = 'none';
        } else {
          alert('Failed to save settings: ' + data.error);
        }
      })
      .catch(err => alert('Save failed: ' + err))
      .finally(() => {
        if (saveBtn) saveBtn.textContent = origText;
      });
  }

  testNotionConnection() {
    const api_key = document.getElementById('notion-token')?.value || '';
    const parent_id = document.getElementById('notion-parent-id')?.value || '';
    const parent_type = document.getElementById('notion-parent-type')?.value || 'database';
    const feedbackDiv = document.getElementById('notion-connection-feedback');

    if (feedbackDiv) {
      feedbackDiv.style.display = 'block';
      feedbackDiv.style.color = 'var(--text-secondary)';
      feedbackDiv.textContent = 'Connecting to Notion API...';
    }

    fetch('/api/notion/test-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key, parent_id, parent_type })
    })
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          if (feedbackDiv) {
            feedbackDiv.style.color = 'var(--green)';
            feedbackDiv.innerHTML = `🟢 <strong>Success!</strong> Connected to "${data.title}"`;
          }
          this.notionConfig.has_key = true;
          this.notionConfig.parent_id = parent_id;
          this.notionConfig.parent_type = parent_type;
          this.updateNotionStatusBadge(true, data.title);
        } else {
          if (feedbackDiv) {
            feedbackDiv.style.color = 'var(--red)';
            feedbackDiv.innerHTML = `🔴 <strong>Connection Failed:</strong> ${data.error}`;
          }
          this.updateNotionStatusBadge(false);
        }
      })
      .catch(err => {
        if (feedbackDiv) {
          feedbackDiv.style.color = 'var(--red)';
          feedbackDiv.textContent = 'Request failed: ' + err;
        }
        this.updateNotionStatusBadge(false);
      });
  }

  updateNotionStatusBadge(forcedConnected = null, itemTitle = '') {
    const badge = document.getElementById('notion-status-badge');
    if (!badge) return;

    const isConnected = forcedConnected !== null ? forcedConnected : this.notionConfig.has_key;

    if (isConnected) {
      badge.textContent = itemTitle ? `Connected to ${itemTitle}` : 'Connected';
      badge.style.background = 'rgba(38,166,154,0.1)';
      badge.style.color = 'var(--green)';
    } else {
      badge.textContent = 'Disconnected';
      badge.style.background = 'rgba(239,83,80,0.1)';
      badge.style.color = 'var(--red)';
    }
  }

  loadNotes(selectId = null) {
    fetch('/api/notes')
      .then(res => res.json())
      .then(notes => {
        this.notes = notes;
        this.renderNotesList();
        
        if (selectId) {
          const matched = notes.find(n => n.id === selectId);
          if (matched) this.selectNote(matched);
        } else if (notes.length > 0 && !this.activeNote) {
          // Default to first note
          this.selectNote(notes[0]);
        } else if (notes.length === 0) {
          this.clearActiveEditor();
        }
      })
      .catch(err => console.error('Failed to load notes:', err));
  }

  renderNotesList() {
    const container = document.getElementById('notes-list-container');
    if (!container) return;

    const query = document.getElementById('note-search')?.value.toLowerCase() || '';
    const symbolFilter = document.getElementById('note-filter-symbol')?.value || '';

    // Filter
    const filtered = this.notes.filter(n => {
      const matchQuery = n.title.toLowerCase().includes(query) || n.content.toLowerCase().includes(query);
      const matchSymbol = !symbolFilter || n.symbol === symbolFilter;
      return matchQuery && matchSymbol;
    });

    if (filtered.length === 0) {
      container.innerHTML = `
        <div class="empty-state" style="padding:40px 10px;">
          <span style="font-size:2rem;">📓</span>
          <p style="font-size:0.75rem; color:var(--text-secondary); margin-top:8px;">No matching notes found.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = filtered.map(n => {
      const isActive = this.activeNote && this.activeNote.id === n.id;
      const activeClass = isActive ? 'style="border:1.5px solid var(--primary); background:rgba(255,255,255,0.3);"' : 'style="background:rgba(255,255,255,0.1);"';
      
      const biasColor = n.sentiment === 'BULLISH' ? 'var(--green)' : n.sentiment === 'BEARISH' ? 'var(--red)' : 'var(--text-muted)';
      const biasIcon = n.sentiment === 'BULLISH' ? '▲' : n.sentiment === 'BEARISH' ? '▼' : '⚪';
      
      const snippet = n.content ? n.content.substring(0, 50).replace(/[#*`|]/g, '') + (n.content.length > 50 ? '...' : '') : 'Empty note text...';
      const isSynced = n.sync_status === 'SYNCED';
      const syncBadge = isSynced 
        ? '<span style="background:rgba(38,166,154,0.15); color:var(--green); font-size:0.6rem; padding:1px 4px; border-radius:4px; font-weight:700;">Synced</span>' 
        : '<span style="background:rgba(0,0,0,0.06); color:var(--text-secondary); font-size:0.6rem; padding:1px 4px; border-radius:4px;">Pending</span>';

      return `
        <div class="note-item-card" ${activeClass} data-id="${n.id}" style="padding:10px; border-radius:var(--radius-sm); cursor:pointer; transition:all 0.2s; border:1px solid var(--border-light);">
          <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px;">
            <strong style="font-size:0.8rem; color:var(--text-primary); line-height:1.2; display:block; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${n.title}</strong>
            ${syncBadge}
          </div>
          <p style="font-size:0.7rem; color:var(--text-secondary); margin:4px 0 8px; line-height:1.3; overflow:hidden; display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;">${snippet}</p>
          <div style="display:flex; justify-content:space-between; align-items:center; font-size:0.65rem;">
            <div style="display:flex; gap:6px; align-items:center;">
              ${n.symbol ? `<span style="background:var(--primary); color:white; padding:1px 4px; border-radius:3px; font-weight:700;">${n.symbol}</span>` : ''}
              <span style="color:${biasColor}; font-weight:700;">${biasIcon} ${n.sentiment}</span>
            </div>
            <span style="color:var(--text-muted); font-size:0.6rem;">${n.updated_at.split(' ')[0]}</span>
          </div>
        </div>
      `;
    }).join('');

    // Bind card clicks
    container.querySelectorAll('.note-item-card').forEach(card => {
      card.addEventListener('click', () => {
        const id = parseInt(card.dataset.id);
        const note = this.notes.find(n => n.id === id);
        if (note) this.selectNote(note);
      });
    });
  }

  selectNote(note) {
    this.activeNote = note;
    this.renderNotesList(); // update active styling

    document.getElementById('note-placeholder-view').style.display = 'none';
    document.getElementById('note-active-view').style.display = 'flex';

    // Parse visual table from note content
    const parsed = this.markdownToVisualTable(note.content || '');
    this.activeTableData = parsed.tableData;

    // Populate editor fields
    document.getElementById('active-note-title').value = note.title;
    document.getElementById('active-note-symbol').value = note.symbol || '';
    document.getElementById('active-note-sentiment').value = note.sentiment;
    
    // Set textarea to remaining text without the raw table block
    document.getElementById('active-note-content').value = parsed.remainingText;
    document.getElementById('note-created-at').textContent = `Created: ${note.created_at}`;

    // Render visual table
    this.renderVisualTableEditor();

    // Reset tabs to edit
    const editTabBtn = document.querySelector('#editor-tabs [data-tab="edit"]');
    if (editTabBtn) editTabBtn.click();

    // Update Notion Sync state info
    const syncDot = document.getElementById('sync-dot');
    const syncText = document.getElementById('sync-text');
    const isSynced = note.sync_status === 'SYNCED';

    if (syncDot && syncText) {
      syncDot.style.background = isSynced ? 'var(--green)' : 'var(--text-secondary)';
      syncText.textContent = isSynced ? 'Synced to Notion' : 'Local Only (Pending Sync)';
    }

    const saveIndicator = document.getElementById('note-save-indicator');
    if (saveIndicator) saveIndicator.style.display = 'none';
  }

  createNewNote() {
    const defaultNote = {
      title: 'New Observation ' + new Date().toLocaleDateString(),
      content: '### Trading Insights\n- Setup:\n- Execution Plan:\n- Key Lessons:',
      symbol: '',
      sentiment: 'NEUTRAL'
    };

    fetch('/api/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(defaultNote)
    })
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          this.loadNotes(data.note.id);
        }
      })
      .catch(err => console.error('Failed to create note:', err));
  }

  saveActiveNote(silent = false) {
    if (!this.activeNote) return;

    const title = document.getElementById('active-note-title').value.strip() || 'Untitled Note';
    const symbol = document.getElementById('active-note-symbol').value || '';
    const sentiment = document.getElementById('active-note-sentiment').value || 'NEUTRAL';
    
    // Get text content
    let content = document.getElementById('active-note-content').value;
    
    // Append visual table markdown if it exists
    if (this.activeTableData) {
      const tableMd = this.visualTableToMarkdown(this.activeTableData);
      if (tableMd) {
        content = content.trim() + '\n' + tableMd;
      }
    }

    const payload = {
      id: this.activeNote.id,
      title,
      symbol,
      sentiment,
      content
    };

    const saveIndicator = document.getElementById('note-save-indicator');
    if (saveIndicator) {
      saveIndicator.style.display = 'inline';
      saveIndicator.textContent = 'Saving...';
    }

    fetch('/api/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          // Update active data silently to avoid cursor disruption
          this.activeNote = data.note;
          
          if (saveIndicator) {
            saveIndicator.textContent = 'Saved locally';
            setTimeout(() => {
              if (saveIndicator.textContent === 'Saved locally') saveIndicator.style.display = 'none';
            }, 2000);
          }

          // Reload note list in background
          fetch('/api/notes')
            .then(r => r.json())
            .then(notes => {
              this.notes = notes;
              this.renderNotesList();
            });
        }
      })
      .catch(err => {
        console.error('Save failed:', err);
        if (saveIndicator) saveIndicator.textContent = 'Save failed!';
      });
  }

  deleteActiveNote() {
    if (!this.activeNote) return;
    if (!confirm('Are you sure you want to delete this note? This action is irreversible.')) return;

    fetch(`/api/notes/${this.activeNote.id}`, { method: 'DELETE' })
      .then(res => res.json())
      .then(data => {
        if (data.success) {
          this.activeNote = null;
          this.loadNotes();
        }
      })
      .catch(err => console.error('Delete failed:', err));
  }

  syncActiveNoteToNotion() {
    if (!this.activeNote) return;

    const syncBtn = document.getElementById('btn-sync-note');
    const origText = syncBtn.innerHTML;
    syncBtn.disabled = true;
    syncBtn.innerHTML = '<span>⏳ Syncing...</span>';

    // Make sure latest content is saved first
    this.saveActiveNote(true);

    fetch(`/api/notes/${this.activeNote.id}/sync`, { method: 'POST' })
      .then(res => {
        if (!res.ok) {
          return res.json().then(e => { throw new Error(e.error || 'Server error') });
        }
        return res.json();
      })
      .then(data => {
        if (data.success) {
          alert('Success! Note has been published and synced to Notion.');
          this.loadNotes(this.activeNote.id);
        }
      })
      .catch(err => {
        alert('Sync Integration Failed: ' + err.message);
      })
      .finally(() => {
        syncBtn.disabled = false;
        syncBtn.innerHTML = origText;
      });
  }

  clearActiveEditor() {
    document.getElementById('note-placeholder-view').style.display = 'flex';
    document.getElementById('note-active-view').style.display = 'none';
  }

  parseMarkdown(text) {
    if (!text) return '<p style="color:var(--text-muted);font-style:italic;">Empty workspace...</p>';
    
    // Escaping simple HTML entities
    let html = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Table Parsing Step
    const lines = html.split('\n');
    let inTable = false;
    let tableHtml = '';
    let processedLines = [];

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (line.startsWith('|') && line.endsWith('|') && line.length > 1) {
        if (!inTable) {
          inTable = true;
          // Check if next line is a separator
          const nextLine = (lines[i + 1] || '').trim();
          const isSeparator = nextLine.startsWith('|') && nextLine.includes('-') && nextLine.endsWith('|');
          
          const cellParts = line.split('|').slice(1, -1);
          let rowCells = cellParts.map(c => `<th>${c.trim()}</th>`).join('');
          
          tableHtml = '<table style="width:100%; border-collapse:collapse; margin:16px 0; font-size:0.8rem; border:1px solid var(--border-light); background:rgba(255,255,255,0.4); border-radius:6px; overflow:hidden;">';
          tableHtml += `<thead style="background:rgba(0,0,0,0.04); border-bottom:2px solid var(--border-light);"><tr>${rowCells}</tr></thead><tbody>`;
          
          if (isSeparator) {
            i++; // Skip the separator line
          }
        } else {
          // Check if separator
          if (line.includes('-') && !line.match(/[a-zA-Z0-9]/)) {
            continue; // Skip isolated separator
          }
          const cellParts = line.split('|').slice(1, -1);
          let rowCells = cellParts.map(c => `<td style="padding:8px 10px; border:1px solid var(--border-light);">${c.trim()}</td>`).join('');
          tableHtml += `<tr style="border-bottom:1px solid var(--border-light);">${rowCells}</tr>`;
        }
      } else {
        if (inTable) {
          inTable = false;
          tableHtml += '</tbody></table>';
          processedLines.push(tableHtml);
        }
        processedLines.push(lines[i]);
      }
    }
    if (inTable) {
      tableHtml += '</tbody></table>';
      processedLines.push(tableHtml);
    }
    html = processedLines.join('\n');

    // Header conversions
    html = html.replace(/^### (.*?)$/gm, '<h4 style="font-family:var(--font-display);font-weight:700;margin-top:14px;color:var(--primary-dark);">$1</h4>');
    html = html.replace(/^## (.*?)$/gm, '<h3 style="font-family:var(--font-display);font-weight:700;margin-top:16px;color:var(--primary-dark);">$1</h3>');
    html = html.replace(/^# (.*?)$/gm, '<h2 style="font-family:var(--font-display);font-weight:800;margin-top:18px;color:var(--primary-dark);border-bottom:1px solid var(--border-light);padding-bottom:4px;">$1</h2>');

    // Bullet lists conversions
    html = html.replace(/^[-*] (.*?)$/gm, '<li style="margin-left:14px;margin-bottom:4px;list-style-type:disc;">$1</li>');

    // Bold conversions
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

    // Image Markdown tag: ![caption](url)
    html = html.replace(/!\[(.*?)\]\((.*?)\)/g, '<img src="$2" alt="$1" style="max-width:100%; max-height:400px; border-radius:8px; border:1px solid var(--border-light); margin:12px 0; display:block; box-shadow: 0 4px 16px rgba(0,0,0,0.06);" />');

    // Line breaks and paragraph wrap
    const paragraphs = html.split('\n');
    html = paragraphs.map(p => {
      const trimmed = p.trim();
      if (!trimmed) return '<div style="height:8px;"></div>';
      if (trimmed.startsWith('<h') || trimmed.startsWith('<li') || trimmed.startsWith('<img') || trimmed.startsWith('<table') || trimmed.startsWith('<thead') || trimmed.startsWith('<tbody') || trimmed.startsWith('<tr') || trimmed.startsWith('<td') || trimmed.startsWith('<th')) return p;
      return `<p style="margin-bottom:8px;">${p}</p>`;
    }).join('\n');

    return html;
  }

  // ── VISUAL TABLE OPERATIONS ──

  visualTableToMarkdown(data) {
    if (!data || !data.headers || data.headers.length === 0) return '';
    let md = '\n';
    md += '| ' + data.headers.map(h => h.trim() || ' ').join(' | ') + ' |\n';
    md += '| ' + data.headers.map(() => '---').join(' | ') + ' |\n';
    data.rows.forEach(row => {
      md += '| ' + row.map(c => c.trim() || ' ').join(' | ') + ' |\n';
    });
    return md;
  }

  markdownToVisualTable(text) {
    if (!text) return { tableData: null, remainingText: '' };
    const lines = text.split('\n');
    let tableLines = [];
    let remainingLines = [];

    const isTableRow = (line) => {
      const trimmed = line.trim();
      return trimmed.startsWith('|') && trimmed.endsWith('|') && trimmed.length > 1;
    };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (isTableRow(line)) {
        tableLines.push(line);
      } else {
        remainingLines.push(line);
      }
    }

    if (tableLines.length === 0) {
      return { tableData: null, remainingText: text };
    }

    // Parse the first found table lines
    let headers = [];
    let rows = [];
    let sepSeen = false;

    tableLines.forEach(line => {
      const parts = line.split('|').slice(1, -1).map(c => c.trim());
      if (headers.length === 0) {
        headers = parts;
      } else if (!sepSeen && line.includes('-') && !line.match(/[a-zA-Z0-9]/)) {
        sepSeen = true;
      } else {
        rows.push(parts);
      }
    });

    return {
      tableData: { headers, rows },
      remainingText: remainingLines.join('\n').trim()
    };
  }

  renderVisualTableEditor() {
    const container = document.getElementById('visual-table-container');
    const wrapper = document.getElementById('visual-table-grid-wrapper');
    if (!container || !wrapper) return;

    if (!this.activeTableData) {
      container.style.display = 'none';
      return;
    }

    container.style.display = 'flex';
    const data = this.activeTableData;

    let headersHtml = data.headers.map((h, colIdx) => `
      <th class="excel-th">
        <input type="text" value="${h}" class="visual-cell-header" data-col="${colIdx}" />
        <span class="btn-visual-del-col" data-col="${colIdx}" title="Delete Column">×</span>
      </th>
    `).join('');

    let rowsHtml = data.rows.map((row, rowIdx) => {
      let cellsHtml = row.map((cell, colIdx) => `
        <td class="excel-td">
          <input type="text" value="${cell}" class="visual-cell-body" data-row="${rowIdx}" data-col="${colIdx}" />
        </td>
      `).join('');

      return `
        <tr class="visual-table-row excel-tr">
          ${cellsHtml}
          <td style="width:24px; border:none !important; text-align:center; padding:0; background:transparent;">
            <span class="btn-visual-del-row" data-row="${rowIdx}" title="Delete Row">×</span>
          </td>
        </tr>
      `;
    }).join('');

    wrapper.innerHTML = `
      <style>
        .excel-table {
          width: 100%;
          border-collapse: collapse;
          background: white;
          border: 1px solid #cbd5e1;
          table-layout: fixed;
          border-radius: 4px;
          overflow: hidden;
        }
        .excel-th, .excel-td {
          border: 1px solid #cbd5e1 !important;
          padding: 0 !important;
          position: relative;
          box-sizing: border-box;
          height: 32px;
        }
        .excel-th {
          background: #f1f5f9 !important;
          font-weight: 700;
          color: #334155;
          text-align: center;
        }
        .excel-td {
          background: #ffffff !important;
        }
        .excel-td:focus-within {
          outline: 2px solid var(--primary) !important;
          outline-offset: -2px;
          z-index: 5;
        }
        .excel-th:focus-within {
          outline: 2px solid var(--primary) !important;
          outline-offset: -2px;
          z-index: 5;
        }
        .excel-table input[type="text"] {
          width: 100%;
          height: 100%;
          border: none !important;
          background: transparent !important;
          padding: 6px 10px !important;
          font-size: 0.78rem !important;
          font-family: inherit !important;
          outline: none !important;
          box-sizing: border-box !important;
          color: var(--text-primary) !important;
        }
        .excel-table th input[type="text"] {
          text-align: center !important;
          font-weight: 700 !important;
        }
        .btn-visual-del-col {
          position: absolute;
          top: 2px;
          right: 4px;
          font-size: 0.8rem;
          color: var(--red);
          cursor: pointer;
          font-weight: bold;
          display: none;
          background: rgba(255,255,255,0.8);
          border-radius: 50%;
          width: 14px;
          height: 14px;
          line-height: 12px;
          text-align: center;
          box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .btn-visual-del-row {
          color: var(--red);
          cursor: pointer;
          font-size: 0.9rem;
          font-weight: bold;
          display: none;
          background: rgba(255,255,255,0.8);
          border-radius: 50%;
          width: 16px;
          height: 16px;
          line-height: 14px;
          text-align: center;
          box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
      </style>
      <table class="excel-table">
        <thead>
          <tr>
            ${headersHtml}
            <th style="width:24px; border:none !important; background:transparent;"></th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    `;

    // Bind Cell Changes
    wrapper.querySelectorAll('.visual-cell-header').forEach(input => {
      input.addEventListener('input', (e) => {
        const col = parseInt(e.target.dataset.col);
        this.activeTableData.headers[col] = e.target.value;
        this.saveActiveNote(true);
      });
    });

    wrapper.querySelectorAll('.visual-cell-body').forEach(input => {
      input.addEventListener('input', (e) => {
        const row = parseInt(e.target.dataset.row);
        const col = parseInt(e.target.dataset.col);
        this.activeTableData.rows[row][col] = e.target.value;
        this.saveActiveNote(true);
      });
    });

    // Hover effect bindings for column deletes
    wrapper.querySelectorAll('th').forEach(th => {
      const delBtn = th.querySelector('.btn-visual-del-col');
      if (delBtn) {
        th.addEventListener('mouseenter', () => { delBtn.style.display = 'inline'; });
        th.addEventListener('mouseleave', () => { delBtn.style.display = 'none'; });
        delBtn.addEventListener('click', () => {
          const colIdx = parseInt(delBtn.dataset.col);
          this.visualDeleteCol(colIdx);
        });
      }
    });

    // Hover effect bindings for row deletes
    wrapper.querySelectorAll('.visual-table-row').forEach(tr => {
      const delBtn = tr.querySelector('.btn-visual-del-row');
      if (delBtn) {
        tr.addEventListener('mouseenter', () => { delBtn.style.display = 'inline'; });
        tr.addEventListener('mouseleave', () => { delBtn.style.display = 'none'; });
        delBtn.addEventListener('click', () => {
          const rowIdx = parseInt(delBtn.dataset.row);
          this.visualDeleteRow(rowIdx);
        });
      }
    });
  }

  addVisualRow() {
    if (!this.activeTableData) return;
    const numCols = this.activeTableData.headers.length;
    const emptyRow = Array(numCols).fill('');
    this.activeTableData.rows.push(emptyRow);
    this.renderVisualTableEditor();
    this.saveActiveNote(true);
  }

  addVisualCol() {
    if (!this.activeTableData) return;
    const nextColNum = this.activeTableData.headers.length + 1;
    this.activeTableData.headers.push(`Header ${nextColNum}`);
    this.activeTableData.rows.forEach(row => row.push(''));
    this.renderVisualTableEditor();
    this.saveActiveNote(true);
  }

  visualDeleteRow(rowIdx) {
    if (!this.activeTableData) return;
    this.activeTableData.rows.splice(rowIdx, 1);
    this.renderVisualTableEditor();
    this.saveActiveNote(true);
  }

  visualDeleteCol(colIdx) {
    if (!this.activeTableData) return;
    if (this.activeTableData.headers.length <= 1) return;
    this.activeTableData.headers.splice(colIdx, 1);
    this.activeTableData.rows.forEach(row => row.splice(colIdx, 1));
    this.renderVisualTableEditor();
    this.saveActiveNote(true);
  }

  removeVisualTable() {
    if (!confirm('Are you sure you want to completely remove this table block?')) return;
    this.activeTableData = null;
    this.renderVisualTableEditor();
    this.saveActiveNote(true);
  }

  createVisualTableBlock() {
    const textarea = document.getElementById('active-note-content');
    const text = textarea ? textarea.value : '';
    const parsed = this.markdownToVisualTable(text);
    
    if (parsed.tableData) {
      this.activeTableData = parsed.tableData;
      if (textarea) textarea.value = parsed.remainingText;
    } else {
      this.activeTableData = {
        headers: ["Strike", "LTP", "Change %"],
        rows: [
          ["24000", "150.00", "+12.4%"],
          ["24100", "95.00", "-8.5%"]
        ]
      };
    }
    this.renderVisualTableEditor();
    this.saveActiveNote(true);
  }
}

// Ensure String.prototype.strip exists
if (!String.prototype.strip) {
  String.prototype.strip = function() {
    return this.trim();
  };
}

// Instantiate globally
window.notionNotes = new NotionNotesWorkspace();
