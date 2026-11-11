// VoiceForever agent window. Talks to Python through window.pywebview.api (see gui/api.py);
// Python pushes connection, game and message events into window.vf.onEvent.
'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') el.className = value;
      else if (key === 'style') el.style.cssText = value;
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
      else if (value === true) el.setAttribute(key, '');
      else if (value !== false && value != null) el.setAttribute(key, value);
    }
    for (const child of children.flat()) if (child != null) el.append(child);
    return el;
  }

  // Static icon markup only; text from Python or the server always goes through textContent.
  function icon(name) {
    const tpl = document.createElement('template');
    tpl.innerHTML = ICONS[name];
    return tpl.content.firstElementChild;
  }

  const ICONS = {
    ok: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="#5ed474" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    wait: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2h7M4.5 14h7M5 2.5c0 3 6 3.5 6 5.5s-6 2.5-6 5.5M11 2.5c0 3-6 3.5-6 5.5s6 2.5 6 5.5" fill="none" stroke="#f2ad42" stroke-width="1.6" stroke-linecap="round"/></svg>',
    bad: '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" stroke="#ff7563" stroke-width="2.2" stroke-linecap="round"/></svg>',
    remove: '<svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  };

  // ------------------------------------------------------------------ words
  const STATES = {
    connected: { title: 'На связи', c1: '#5ed474', c2: '#1d6b2c', glow: 'rgba(94,212,116,.55)' },
    connecting: { title: 'Подключение…', c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.5)', pulse: true },
    reconnecting: { title: 'Связь потеряна', c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.45)' },
    disconnected: { title: 'Отключено', c1: '#a59a88', c2: '#3a342b', glow: 'rgba(0,0,0,0)' },
    auth_failed: { title: 'Токен не принят', c1: '#ff7563', c2: '#7a1a10', glow: 'rgba(255,117,99,.5)' },
    replaced: { title: 'Агент на другом ПК', c1: '#f2ad42', c2: '#7a4a0c', glow: 'rgba(242,173,66,.35)' },
  };

  const GAME = {
    active: { label: 'окно активно', color: '#5ed474', hint: () => 'Сообщения сразу уходят в чат.' },
    background: { label: 'в фоне', color: '#f2ad42', hint: () => `Сообщение подождёт до ${focusWait()} с, пока ты вернёшься в игру.` },
    closed: { label: 'не запущена', color: '#a59a88', hint: () => 'Агент найдёт окно, когда игра запустится.' },
  };

  const CHANNEL_NAMES = { 1: 'Общий', 2: 'Торговля' };

  function channelLabel(channel, target) {
    switch (channel) {
      case 'say': return ['Сказать', '#f3eee6'];
      case 'yell': return ['Крик', '#ff6a5a'];
      case 'emote': return ['Эмоция', '#ff9a5a'];
      case 'group': case 'party': return ['Группа', '#aab4ff'];
      case 'raid': return ['Рейд', '#ff9a4a'];
      case 'raid_warning': return ['Объявление рейду', '#ff7a3a'];
      case 'instance': return ['Подземелье', '#ff9a4a'];
      case 'guild': return ['Гильдия', '#56e06a'];
      case 'officer': return ['Офицер', '#48c858'];
      case 'whisper': return [target ? `Шёпот → ${target}` : 'Шёпот', '#ff8ef0'];
      case 'reply': return ['Ответ на шёпот', '#ff8ef0'];
      case 'channel': return [CHANNEL_NAMES[target] ? `${target}. ${CHANNEL_NAMES[target]}` : `Канал ${target}`, '#ffc8b0'];
      default: return [channel, '#f3eee6'];
    }
  }

  const KEY_LABELS = { ctrl: 'Ctrl', control: 'Ctrl', shift: 'Shift', alt: 'Alt', pageup: 'PageUp', pagedown: 'PageDown', backspace: 'Backspace', esc: 'Esc' };
  const keyLabel = (key) => KEY_LABELS[key] || (/^num\d$/.test(key) ? `Num ${key.slice(3)}` : key.length <= 3 ? key.toUpperCase() : key[0].toUpperCase() + key.slice(1));

  const CODE_KEYS = { Home: 'home', End: 'end', Insert: 'insert', Delete: 'delete', PageUp: 'pageup', PageDown: 'pagedown', Space: 'space', Tab: 'tab', Backspace: 'backspace', Enter: 'enter' };
  function keyFromCode(code) {
    let m;
    if ((m = /^F(\d{1,2})$/.exec(code)) && +m[1] >= 1 && +m[1] <= 24) return `f${m[1]}`;
    if ((m = /^Key([A-Z])$/.exec(code))) return m[1].toLowerCase();
    if ((m = /^Digit(\d)$/.exec(code))) return m[1];
    if ((m = /^Numpad(\d)$/.exec(code))) return `num${m[1]}`;
    return CODE_KEYS[code] || null;
  }

  // The same combo as a WoW binding for "/vf key": modifiers in WoW's order (ALT-CTRL-SHIFT).
  const WOW_MODIFIERS = ['alt', 'ctrl', 'shift'];
  const WOW_KEYS = { control: 'ctrl', return: 'enter', esc: 'escape' };
  function wowBinding(keys) {
    const names = keys.map((k) => WOW_KEYS[k] || k);
    const mods = WOW_MODIFIERS.filter((m) => names.includes(m));
    const rest = names.filter((k) => !WOW_MODIFIERS.includes(k)).map((k) => k.replace(/^num(\d)$/, 'numpad$1'));
    return [...mods, ...rest].join('-').toUpperCase();
  }

  const TEST_HINT = 'Строка уйдёт в /say, когда окно WoW станет активным.';

  // ------------------------------------------------------------------ state
  const S = {
    ready: false,
    pending: [],
    seq: 0,
    configured: false,
    saved: null, // the settings as written to config.toml
    form: null, // the settings being edited (wizard and settings tab share it)
    defaults: null,
    configPath: '',
    status: { state: 'disconnected', detail: '', attempt: 0, retry_at: null, since: null },
    game: 'closed',
    history: [],
    view: 'status',
    wizard: { step: 1, check: 'idle', detail: '' },
    capturing: false,
    actionsFor: null,
    savedNote: '', // shown in the save bar right after saving
  };

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const sameSettings = (a, b) => Object.keys({ ...a, ...b }).every((k) => JSON.stringify(a[k]) === JSON.stringify(b[k]));
  const focusWait = () => Math.round((S.saved || S.form || {}).focus_wait_s || 15);
  const api = () => window.pywebview.api;

  async function call(name, ...args) {
    try {
      return await api()[name](...args);
    } catch (error) {
      console.error(name, error);
      return null;
    }
  }

  // ------------------------------------------------------------------ start
  async function init() {
    if (init.started || !window.pywebview || !window.pywebview.api) return;
    init.started = true;
    const state = await call('get_state');
    if (!state) return;
    S.seq = state.seq;
    S.configured = state.configured;
    S.saved = state.config;
    S.form = clone(state.config);
    S.defaults = state.defaults;
    S.configPath = state.config_path;
    S.status = state.status;
    S.game = state.game;
    S.history = state.history;
    $('#version').textContent = `агент ${state.version}`;

    bindControls();
    fillForm();
    renderStatus();
    renderGame();
    renderMessages();
    $('#st-test-hint').textContent = TEST_HINT;

    if (state.first_run) {
      showErrors(state.errors);
      showView('wizard');
      renderWizard();
    } else {
      showErrors(state.errors);
      showView(Object.keys(state.errors).length ? 'settings' : 'status');
    }
    S.ready = true;
    for (const event of S.pending.splice(0)) onEvent(event);
    setInterval(renderStatusLine, 1000);
  }

  function onEvent(event) {
    if (!S.ready) {
      S.pending.push(event);
      return;
    }
    if (event.seq <= S.seq) return; // already part of the snapshot
    S.seq = event.seq;
    switch (event.type) {
      case 'status':
        S.status = event.status;
        renderStatus();
        wizardOnStatus();
        break;
      case 'delivery':
        S.history.unshift(event.delivery);
        S.history.length = Math.min(S.history.length, 50);
        renderMessages();
        break;
      case 'game':
        S.game = event.game;
        renderGame();
        break;
    }
  }

  window.vf = { onEvent };
  window.addEventListener('pywebviewready', init);
  init();

  // ------------------------------------------------------------------ views
  function showView(name) {
    S.view = name;
    for (const view of ['wizard', 'status', 'settings']) $(`#view-${view}`).hidden = view !== name;
    $('#tabs').hidden = name === 'wizard';
    for (const tab of $$('.tab')) {
      const on = tab.dataset.tab === name;
      tab.classList.toggle('on', on);
      if (on) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    }
  }

  function showErrors(errors = {}) {
    for (const el of $$('[data-error-for]')) el.textContent = errors[el.dataset.errorFor] || '';
    for (const input of $$('[data-field]')) input.classList.toggle('bad', Boolean(errors[input.dataset.field]));
    const fileError = $('#s-file-error');
    fileError.textContent = errors.file || '';
    fileError.hidden = !errors.file;
  }

  // ------------------------------------------------------------------ status tab
  function renderStatus() {
    const s = S.status;
    const look = STATES[s.state] || STATES.disconnected;
    const orb = $('#st-orb');
    orb.style.background = `radial-gradient(circle at 36% 30%, rgba(255,255,255,.75) 0 7%, ${look.c1} 34%, ${look.c2} 100%)`;
    orb.style.boxShadow = `0 0 0 3px #0b0907, 0 0 0 5px #7d6130, 0 0 0 6px #000, 0 0 26px ${look.glow}`;
    orb.classList.toggle('pulse', Boolean(look.pulse));
    const title = $('#st-title');
    title.textContent = look.title;
    title.style.color = look.c1;
    $('#st-url').textContent = S.saved && S.saved.server_url ? S.saved.server_url : 'Адрес сервера ещё не задан';
    renderStatusLine();
    const key = `${s.state}|${S.configured}`;
    if (S.actionsFor !== key) {
      S.actionsFor = key;
      $('#st-actions').replaceChildren(...statusActions(s.state));
    }
  }

  function statusLine(s) {
    const now = Date.now() / 1000;
    const detail = s.detail ? s.detail[0].toUpperCase() + s.detail.slice(1) : '';
    switch (s.state) {
      case 'connected': {
        const up = Math.max(0, Math.floor(now - (s.since || now)));
        const hms = `${Math.floor(up / 3600)}:${String(Math.floor((up % 3600) / 60)).padStart(2, '0')}:${String(up % 60).padStart(2, '0')}`;
        return `В сети ${hms} · режим: ${S.saved && S.saved.mode === 'chat' ? 'обычный чат' : 'аддон'}`;
      }
      case 'connecting': return 'Открываю соединение, жду ответа до 15 с';
      case 'reconnecting': {
        const wait = Math.max(0, Math.ceil((s.retry_at || now) - now));
        return `${detail}. Повтор через ${wait} с, попытка ${s.attempt}`;
      }
      case 'auth_failed': return `${detail}. Проверь токен в настройках`;
      case 'replaced': return 'С этим токеном подключились с другого компьютера';
      default:
        if (!S.configured) return 'Сначала заполни и сохрани настройки';
        return detail || 'Сообщения из Telegram сейчас не дойдут до игры';
    }
  }

  function renderStatusLine() {
    $('#st-line').textContent = statusLine(S.status);
  }

  function statusActions(state) {
    const button = (label, ghost, action, disabled = false) =>
      h('button', { class: ghost ? 'btn ghost' : 'btn', type: 'button', style: 'flex: 1', disabled, onclick: action }, label);
    const connect = () => call('connect');
    switch (state) {
      case 'connected':
        return [button('Отключить', true, () => call('disconnect')), button('Переподключить', true, () => call('reconnect'))];
      case 'connecting':
        return [button('Отменить', true, () => call('disconnect'))];
      case 'reconnecting':
        return [button('Повторить сейчас', false, () => call('reconnect')), button('Отключить', true, () => call('disconnect'))];
      case 'auth_failed':
        return [button('Открыть настройки', false, () => { showView('settings'); $('#s-token').focus(); }), button('Повторить', true, connect)];
      case 'replaced':
        return [button('Подключить здесь', false, connect)];
      default:
        return [button('Подключить', false, connect, !S.configured)];
    }
  }

  function renderGame() {
    const game = GAME[S.game] || GAME.closed;
    const pill = $('#game-pill');
    pill.textContent = game.label;
    pill.style.color = game.color;
    pill.style.borderColor = game.color;
    $('#game-hint').textContent = game.hint();
  }

  function renderMessages() {
    const list = $('#msgs');
    if (!S.history.length) {
      list.replaceChildren(h('li', { class: 'hint empty' }, 'Пока пусто. Скажи что-нибудь боту в Telegram или нажми «Тест в /say».'));
      return;
    }
    list.replaceChildren(...S.history.map((d) => {
      const [label, color] = channelLabel(d.channel, d.target);
      const time = new Date(d.at * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      let mark = 'ok';
      let note = null;
      let label2 = 'Отправлено в игру';
      if (d.status === 'not_focused') {
        mark = 'wait';
        label2 = 'Не отправлено: окно WoW не было активно';
        note = h('span', { class: 'mdetail warn' }, `Окно WoW не стало активным за ${focusWait()} с, сообщение не ушло.`);
      } else if (d.status !== 'ok') {
        mark = 'bad';
        label2 = 'Ошибка ввода';
        note = h('span', { class: 'mdetail bad' }, `Не получилось ввести: ${d.detail || 'неизвестная ошибка'}`);
      }
      return h('li', { class: 'msg' },
        h('span', { class: 'time' }, time),
        h('div', { class: 'field', style: 'gap: 2px; min-width: 0' },
          h('span', { class: 'mtext', style: `color: ${color}` }, h('b', {}, `[${label}]`), ` ${d.text}`),
          note),
        h('span', { role: 'img', 'aria-label': label2, title: label2, style: 'padding-top: 2px' }, icon(mark)));
    }));
  }

  async function runTest(button, onTick, onDone) {
    button.disabled = true;
    let left = focusWait();
    onTick(left);
    const timer = setInterval(() => onTick((left = Math.max(0, left - 1))), 1000);
    const result = (await call('send_test')) || { status: 'error', detail: 'агент не ответил' };
    clearInterval(timer);
    button.disabled = false;
    onDone(result);
  }

  // ------------------------------------------------------------------ first-run wizard
  function renderWizard() {
    const { step, check } = S.wizard;
    for (const li of $$('#view-wizard .steps li')) {
      const n = Number(li.dataset.step);
      li.classList.toggle('on', n === step);
      li.classList.toggle('done', n < step);
      const mark = $('.sn', li);
      if (n < step) mark.replaceChildren(icon('ok'));
      else mark.textContent = String(n);
    }
    for (const page of $$('#view-wizard [data-page]')) page.hidden = Number(page.dataset.page) !== step;
    $('#w-back').hidden = step === 1;
    $('#w-skip').hidden = !(step === 1 && (check === 'failed' || check === 'replaced'));
    const next = $('#w-next');
    next.textContent = ['Подключиться', 'Далее', 'Готово'][step - 1];
    next.disabled = step === 1 && check === 'checking';
    $$('[data-focus-wait]').forEach((el) => { el.textContent = focusWait(); });
    renderCheck();
  }

  function renderCheck() {
    const { check, detail } = S.wizard;
    const box = $('#w-check');
    box.hidden = check === 'idle';
    const text = {
      checking: 'Подключаюсь к серверу, жду ответа до 15 секунд…',
      ok: 'Сервер ответил, токен принят.',
      auth: `Сервер не принял токен (${detail}). Сверь его со строкой VF_USERS на сервере.`,
      failed: `Не удалось подключиться: ${detail}. Агент будет пробовать сам, настройку можно продолжить.`,
      replaced: 'С этим токеном только что подключился другой агент.',
    }[check];
    const kind = { checking: 'warn', ok: 'good', auth: 'bad', failed: 'bad', replaced: 'warn' }[check];
    box.className = `note ${kind || ''}`;
    if (check === 'checking') {
      box.replaceChildren(h('span', { class: 'pulse', style: 'width: 12px; height: 12px; flex: none; border-radius: 50%; background: #f2ad42; box-shadow: 0 0 10px rgba(242,173,66,.8)' }), h('span', {}, text));
    } else if (text) {
      box.replaceChildren(icon(kind === 'good' ? 'ok' : kind === 'bad' ? 'bad' : 'wait'), h('span', {}, text));
    }
  }

  function setCheck(check, detail = '') {
    S.wizard.check = check;
    S.wizard.detail = detail;
    renderWizard();
  }

  function wizardOnStatus() {
    if (S.view !== 'wizard' || S.wizard.step !== 1 || !['checking', 'failed', 'replaced'].includes(S.wizard.check)) return;
    const s = S.status;
    if (s.state === 'connected') {
      setCheck('ok');
      setTimeout(() => {
        if (S.wizard.step === 1) {
          S.wizard.step = 2;
          renderWizard();
        }
      }, 900);
    } else if (s.state === 'auth_failed') setCheck('auth', s.detail);
    else if (s.state === 'reconnecting') setCheck('failed', s.detail);
    else if (s.state === 'replaced') setCheck('replaced');
  }

  async function saveForm() {
    const result = await call('save_config', S.form);
    if (!result) return null;
    showErrors(result.errors);
    if (result.ok) applySaved(result.config);
    return result;
  }

  async function wizardNext() {
    const { step } = S.wizard;
    if (step === 1) {
      setCheck('checking');
      const result = await saveForm();
      if (!result || !result.ok) {
        setCheck('idle');
        return;
      }
      if (!result.reconnecting) await call('reconnect');
    } else if (step === 2) {
      const result = await saveForm();
      if (result && result.ok) {
        S.wizard.step = 3;
        renderWizard();
      }
    } else {
      S.form.connect_on_start = $('#w-auto').checked;
      S.form.autostart = $('#w-boot').checked;
      const result = await saveForm();
      if (result && result.ok) {
        showView('status');
        if (S.status.state === 'disconnected' && S.saved.connect_on_start) call('connect');
      }
    }
  }

  async function wizardTest() {
    const box = $('#w-test-result');
    const meter = h('div', { style: 'width: 100%' });
    const label = h('span');
    box.hidden = false;
    box.className = 'note warn col';
    box.replaceChildren(label, h('div', { class: 'meter' }, meter));
    const total = focusWait();
    await runTest($('#w-test'), (left) => {
      label.textContent = `Жду окно WoW: осталось ${left} с`;
      meter.style.width = `${Math.round((left / total) * 100)}%`;
    }, (result) => {
      box.className = `note ${result.status === 'ok' ? 'good' : result.status === 'not_focused' ? 'warn' : 'bad'}`;
      const text = result.status === 'ok' ? 'Строка ушла в /say. Можно играть.'
        : result.status === 'not_focused' ? `Окно WoW не стало активным за ${total} с. Попробуй ещё раз.`
        : `Не получилось: ${result.detail}`;
      box.replaceChildren(icon(result.status === 'ok' ? 'ok' : result.status === 'not_focused' ? 'wait' : 'bad'), h('span', {}, text));
    });
  }

  // ------------------------------------------------------------------ settings tab
  function fillForm() {
    for (const input of $$('[data-field]')) input.value = S.form[input.dataset.field] ?? '';
    for (const range of $$('[data-range]')) {
      range.value = S.form[range.dataset.range];
      showRange(range);
    }
    for (const box of $$('[data-check]')) box.checked = Boolean(S.form[box.dataset.check]);
    $('#w-auto').checked = Boolean(S.form.connect_on_start);
    $('#w-boot').checked = Boolean(S.form.autostart);
    $('#s-path').textContent = S.configPath;
    renderChips();
    renderMode();
    renderHotkey();
    renderDirty();
  }

  function showRange(range) {
    $(`output[for="${range.id}"]`).textContent = `${S.form[range.dataset.range]} ${range.dataset.unit}`;
  }

  function applySaved(config) {
    S.saved = config;
    S.form = clone(config);
    S.configured = true;
    fillForm();
    renderStatus();
    renderGame();
  }

  function renderDirty() {
    const dirty = !sameSettings(S.form, S.saved);
    const line = $('#s-dirty');
    line.textContent = dirty ? 'Есть несохранённые изменения'
      : !S.configured ? 'Настройки ещё не сохранены'
      : S.savedNote || 'Все изменения сохранены';
    line.classList.toggle('good', !dirty && Boolean(S.savedNote));
    $('#s-save').disabled = !dirty && S.configured;
    $('#s-cancel').disabled = !dirty;
  }

  function changed() {
    renderDirty();
    S.savedNote = '';
  }

  function renderMode() {
    const mode = S.form.mode;
    for (const button of $$('[data-mode]')) {
      const on = button.dataset.mode === mode;
      button.classList.toggle('sel', on);
      button.setAttribute('aria-pressed', String(on));
    }
    for (const el of $$('[data-addon-only]')) el.hidden = mode !== 'addon';
    $('#s-mode-hint').textContent = mode === 'addon'
      ? 'Чат не открывается, аддон сам выбирает группу, рейд или подземелье.'
      : 'Без аддона: агент открывает чат клавишей Enter и печатает /p, /g, /w.';
  }

  function renderHotkey() {
    const keys = String(S.form.addon_hotkey || '').split('+').filter(Boolean);
    for (const box of $$('[data-hotkey]')) {
      if (S.capturing) {
        box.replaceChildren(h('span', { class: 'capturing pulse' }, 'Нажми сочетание клавиш… Esc отменит'));
      } else {
        box.replaceChildren(...keys.flatMap((key, i) => [...(i ? [h('span', {}, '+')] : []), h('span', { class: 'key' }, keyLabel(key))]));
      }
    }
    for (const button of $$('[data-capture]')) button.disabled = S.capturing;
    const changedKey = S.saved && S.form.addon_hotkey !== S.saved.addon_hotkey;
    for (const hint of $$('[data-hotkey-hint]')) {
      hint.textContent = changedKey
        ? `Поменяй клавишу и в игре: /vf key ${wowBinding(keys)}`
        : 'Должна совпадать с клавишей, которую показывает /vf в игре. Аддон кладётся в папку Interface\\AddOns.';
    }
  }

  function renderChips() {
    for (const box of $$('[data-chips]')) {
      const field = box.dataset.chips;
      const items = S.form[field] || [];
      box.replaceChildren(
        ...items.map((name) => h('span', { class: 'chip' }, name,
          h('button', {
            type: 'button',
            'aria-label': `Убрать ${name}`,
            onclick: () => {
              S.form[field] = S.form[field].filter((x) => x !== name);
              renderChips();
              changed();
            },
          }, icon('remove')))),
        chipAdder(field));
    }
  }

  function chipAdder(field) {
    const button = h('button', { class: 'addchip', type: 'button' }, '+ Добавить');
    button.addEventListener('click', () => {
      const input = h('input', { class: 'inp chipinput', type: 'text', spellcheck: 'false', 'aria-label': 'Новое значение' });
      let done = false;
      const finish = (keep) => {
        if (done) return;
        done = true;
        const value = input.value.trim();
        if (keep && value && !S.form[field].includes(value)) {
          S.form[field] = [...S.form[field], value];
          changed();
        }
        renderChips();
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') finish(true);
        else if (e.key === 'Escape') finish(false);
      });
      input.addEventListener('blur', () => finish(true));
      button.replaceWith(input);
      input.focus();
    });
    return button;
  }

  async function detectGame() {
    const button = $('#s-detect');
    const note = $('#s-detect-note');
    button.disabled = true;
    $('#s-detect-hint').hidden = true;
    note.hidden = false;
    note.className = 'note warn';
    let left = 5;
    note.textContent = `Переключись в WoW. Запомню активное окно через ${left} с.`;
    const timer = setInterval(() => {
      left = Math.max(1, left - 1);
      note.textContent = `Переключись в WoW. Запомню активное окно через ${left} с.`;
    }, 1000);
    const found = (await call('detect_game_window')) || { title: '', exe: '' };
    clearInterval(timer);
    button.disabled = false;
    if (!found.title && !found.exe) {
      note.className = 'note bad';
      note.textContent = 'Активным осталось окно агента. Нажми ещё раз и успей переключиться в игру.';
      return;
    }
    let added = false;
    for (const [field, value] of [['window_titles', found.title], ['process_names', found.exe]]) {
      if (value && !S.form[field].some((x) => x.toLowerCase() === value.toLowerCase())) {
        S.form[field] = [...S.form[field], value];
        added = true;
      }
    }
    renderChips();
    changed();
    note.className = 'note good';
    note.textContent = `Нашёл «${found.title || 'без названия'}», процесс ${found.exe || 'неизвестен'}. `
      + (added ? 'Добавил в списки, не забудь сохранить.' : 'Оба уже в списке.');
  }

  async function saveSettings() {
    const wasConfigured = S.configured;
    const result = await saveForm();
    if (!result) return;
    if (!result.ok) {
      const first = $$('#view-settings [data-error-for], #s-file-error').find((el) => el.textContent);
      if (first) (first.closest('.field') || first).scrollIntoView({ block: 'center', behavior: 'smooth' });
      return;
    }
    S.savedNote = result.reconnecting ? 'Сохранено. Переподключаюсь с новыми настройками…' : 'Сохранено в config.toml';
    renderDirty();
    clearTimeout(saveSettings.timer);
    saveSettings.timer = setTimeout(() => { S.savedNote = ''; renderDirty(); }, 6000);
    if (!wasConfigured && S.status.state === 'disconnected' && S.saved.connect_on_start) call('connect');
  }

  // ------------------------------------------------------------------ wiring
  function bindControls() {
    $('#btn-minimize').addEventListener('click', () => call('minimize'));
    $('#btn-close').addEventListener('click', () => call('close'));
    for (const tab of $$('.tab')) tab.addEventListener('click', () => showView(tab.dataset.tab));

    for (const input of $$('[data-field]')) {
      input.addEventListener('input', () => {
        S.form[input.dataset.field] = input.value;
        for (const twin of $$(`[data-field="${input.dataset.field}"]`)) if (twin !== input) twin.value = input.value;
        if (S.view === 'wizard' && S.wizard.check !== 'checking') setCheck('idle');
        changed();
      });
    }
    for (const id of ['#w-url', '#w-token']) {
      $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter' && !$('#w-next').disabled) wizardNext(); });
    }
    for (const button of $$('[data-reveal]')) {
      button.addEventListener('click', () => {
        const input = $(`#${button.dataset.reveal}`);
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        button.textContent = show ? 'Скрыть' : 'Показать';
      });
    }
    for (const range of $$('[data-range]')) {
      range.addEventListener('input', () => {
        S.form[range.dataset.range] = Number(range.value);
        showRange(range);
        changed();
      });
    }
    for (const box of $$('[data-check]')) {
      box.addEventListener('change', () => {
        S.form[box.dataset.check] = box.checked;
        changed();
      });
    }
    for (const button of $$('[data-mode]')) {
      button.addEventListener('click', () => {
        S.form.mode = button.dataset.mode;
        renderMode();
        changed();
      });
    }
    for (const button of $$('[data-capture]')) {
      button.addEventListener('click', () => {
        S.capturing = true;
        renderHotkey();
      });
    }
    document.addEventListener('keydown', (e) => {
      if (!S.capturing) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.code === 'Escape' && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        S.capturing = false;
        renderHotkey();
        return;
      }
      const key = keyFromCode(e.code);
      if (!key) return; // a modifier on its own: wait for the main key
      S.form.addon_hotkey = [e.altKey && 'alt', e.ctrlKey && 'ctrl', e.shiftKey && 'shift', key].filter(Boolean).join('+');
      S.capturing = false;
      renderHotkey();
      changed();
    }, true);

    $('#w-next').addEventListener('click', wizardNext);
    $('#w-back').addEventListener('click', () => {
      S.wizard.step = Math.max(1, S.wizard.step - 1);
      renderWizard();
    });
    $('#w-skip').addEventListener('click', () => {
      S.wizard.step = 2;
      renderWizard();
    });
    $('#w-test').addEventListener('click', wizardTest);

    $('#st-test').addEventListener('click', () => runTest($('#st-test'),
      (left) => { $('#st-test-hint').textContent = `Жду окно WoW: осталось ${left} с`; },
      () => { $('#st-test-hint').textContent = TEST_HINT; }));
    $('#btn-log').addEventListener('click', () => call('open_log'));

    $('#s-detect').addEventListener('click', detectGame);
    $('#s-reset-timings').addEventListener('click', () => {
      for (const key of ['focus_wait_s', 'open_delay_ms', 'part_delay_ms', 'key_delay_ms']) S.form[key] = S.defaults[key];
      for (const range of $$('[data-range]')) {
        range.value = S.form[range.dataset.range];
        showRange(range);
      }
      changed();
    });
    $('#s-open-folder').addEventListener('click', () => call('open_config_folder'));
    $('#s-cancel').addEventListener('click', () => {
      S.form = clone(S.saved);
      fillForm();
      showErrors({});
    });
    $('#s-save').addEventListener('click', saveSettings);
  }
})();
