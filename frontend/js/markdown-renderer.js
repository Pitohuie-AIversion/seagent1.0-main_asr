(function (global) {
  'use strict';

  const ASSISTANT_ROLES = new Set(['assistant', 'bot']);
  const ALLOWED_TAGS = [
    'p', 'br', 'strong', 'em', 'del',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
    'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr'
  ];
  const ALLOWED_ATTRIBUTES = ['href', 'title'];
  const SAFE_LINK_PATTERN = /^(?:(?:https?|mailto):[^\s]*|(?:[#/?]|\.\.?\/)[^\s]*)$/i;

  const normalizeText = (value) => String(value ?? '').replace(/\r\n?/g, '\n');

  const escapeHtml = (value) => normalizeText(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[character]);

  const renderPlainText = (value) => escapeHtml(value).replace(/\n/g, '<br>');

  const buildMarkdownRenderer = () => {
    const renderer = new global.marked.Renderer();

    // Raw HTML is displayed literally. It is never accepted as authored markup.
    renderer.html = ({ text }) => escapeHtml(text);
    // Images are deliberately disabled; keep useful alt text without loading a URL.
    renderer.image = ({ text }) => escapeHtml(text || '');
    return renderer;
  };

  const renderAssistantMarkdown = (value) => {
    const source = normalizeText(value);
    const parserReady = global.marked
      && typeof global.marked.parse === 'function'
      && typeof global.marked.Renderer === 'function';
    const sanitizerReady = global.DOMPurify
      && typeof global.DOMPurify.sanitize === 'function';

    if (!parserReady || !sanitizerReady) {
      return renderPlainText(source);
    }

    try {
      const parsed = global.marked.parse(source, {
        async: false,
        breaks: true,
        gfm: true,
        renderer: buildMarkdownRenderer()
      });
      if (typeof parsed !== 'string') {
        return renderPlainText(source);
      }

      return global.DOMPurify.sanitize(parsed, {
        ALLOWED_TAGS,
        ALLOWED_ATTR: ALLOWED_ATTRIBUTES,
        ALLOWED_URI_REGEXP: SAFE_LINK_PATTERN,
        ALLOW_ARIA_ATTR: false,
        ALLOW_DATA_ATTR: false,
        ALLOW_UNKNOWN_PROTOCOLS: false,
        FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'img', 'svg', 'math'],
        FORBID_ATTR: ['style', 'src', 'srcset']
      });
    } catch (_error) {
      return renderPlainText(source);
    }
  };

  const render = (value, role) => {
    if (!ASSISTANT_ROLES.has(String(role || '').toLowerCase())) {
      return renderPlainText(value);
    }
    return renderAssistantMarkdown(value);
  };

  global.SEAgentMarkdown = Object.freeze({
    escapeHtml,
    render,
    renderPlainText
  });
})(window);
