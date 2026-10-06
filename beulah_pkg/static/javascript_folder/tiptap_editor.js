import { Editor } from 'https://esm.sh/@tiptap/core@2';
import StarterKit from 'https://esm.sh/@tiptap/starter-kit@2';
import Underline from 'https://esm.sh/@tiptap/extension-underline@2';
import Link from 'https://esm.sh/@tiptap/extension-link@2';
import Image from 'https://esm.sh/@tiptap/extension-image@2';
import TextAlign from 'https://esm.sh/@tiptap/extension-text-align@2';

const editors = new Map();

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}

function cleanEmptyParagraphs(html) {
  return html.replace(/<p><\/p>/g, '').replace(/<p><br><\/p>/g, '').trim();
}

async function uploadImage(editor, editorElement, toolbar) {
  const input = toolbar.querySelector('.tiptap-image-input');
  if (!input) {
    return;
  }

  input.value = '';
  input.click();
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) {
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      window.alertError ? window.alertError('Error', 'Image must be under 5MB.') : alert('Image must be under 5MB.');
      return;
    }

    const uploadUrl = editorElement.dataset.uploadUrl;
    if (!uploadUrl) {
      const url = prompt('Image URL');
      if (url) {
        editor.chain().focus().setImage({ src: url }).run();
      }
      return;
    }

    const form = new FormData();
    form.append('image', file);

    try {
      const response = await fetch(uploadUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken() },
        body: form,
      });
      const data = await response.json();
      if (!response.ok || !data.success) {
        throw new Error(data.message || 'Image upload failed.');
      }
      editor.chain().focus().setImage({ src: data.url, alt: file.name }).run();
    } catch (error) {
      window.alertError ? window.alertError('Error', error.message) : alert(error.message);
    }
  };
}

function runCommand(command, editor, editorElement, toolbar) {
  const chain = editor.chain().focus();

  if (command === 'undo') editor.commands.undo();
  if (command === 'redo') editor.commands.redo();
  if (command === 'bold') chain.toggleBold().run();
  if (command === 'italic') chain.toggleItalic().run();
  if (command === 'underline') chain.toggleUnderline().run();
  if (command === 'strike') chain.toggleStrike().run();
  if (command === 'align-left') chain.setTextAlign('left').run();
  if (command === 'align-center') chain.setTextAlign('center').run();
  if (command === 'align-right') chain.setTextAlign('right').run();
  if (command === 'bullet-list') chain.toggleBulletList().run();
  if (command === 'ordered-list') chain.toggleOrderedList().run();
  if (command === 'blockquote') chain.toggleBlockquote().run();
  if (command === 'code-block') chain.toggleCodeBlock().run();
  if (command === 'horizontal-rule') chain.setHorizontalRule().run();
  if (command === 'clear-formatting') chain.clearNodes().unsetAllMarks().run();
  if (command === 'link') {
    const previousUrl = editor.getAttributes('link').href || '';
    const url = prompt('Enter link URL', previousUrl);
    if (url === null) return;
    if (url.trim() === '') {
      chain.unsetLink().run();
      return;
    }
    chain.setLink({ href: url.trim(), target: '_blank', rel: 'noopener noreferrer' }).run();
  }
  if (command === 'image') {
    uploadImage(editor, editorElement, toolbar);
  }
}

function initEditor(editorElement) {
  const editorId = editorElement.id;
  const toolbar = document.querySelector(`[data-tiptap-toolbar="${editorId}"]`);
  const initialContent = editorElement.innerHTML.trim();

  const editor = new Editor({
    element: editorElement,
    extensions: [
      StarterKit,
      Underline,
      Link.configure({ openOnClick: false, autolink: true }),
      Image,
      TextAlign.configure({ types: ['heading', 'paragraph'] }),
    ],
    content: initialContent || '',
    editorProps: {
      attributes: {
        class: 'tiptap-prose',
      },
    },
  });

  if (toolbar) {
    toolbar.addEventListener('click', (event) => {
      const button = event.target.closest('[data-command]');
      if (!button || button.tagName === 'SELECT') {
        return;
      }
      event.preventDefault();
      runCommand(button.dataset.command, editor, editorElement, toolbar);
    });

    toolbar.addEventListener('change', (event) => {
      const select = event.target.closest('select[data-command="block"]');
      if (!select) {
        return;
      }
      const value = select.value;
      if (value === 'paragraph') editor.chain().focus().setParagraph().run();
      if (value === 'heading-1') editor.chain().focus().toggleHeading({ level: 1 }).run();
      if (value === 'heading-2') editor.chain().focus().toggleHeading({ level: 2 }).run();
      if (value === 'heading-3') editor.chain().focus().toggleHeading({ level: 3 }).run();
      select.value = 'paragraph';
    });
  }

  editors.set(editorId, editor);
}

window.BeulahTiptap = {
  get(editorId = 'editor') {
    return editors.get(editorId);
  },
  getData(editorId = 'editor') {
    const editor = editors.get(editorId);
    if (!editor) {
      return { html: '', json: null, text: '' };
    }
    return {
      html: cleanEmptyParagraphs(editor.getHTML()),
      json: editor.getJSON(),
      text: editor.getText().trim(),
    };
  },
};

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-tiptap-editor]').forEach(initEditor);
  document.dispatchEvent(new CustomEvent('beulah:tiptap-ready'));
});
