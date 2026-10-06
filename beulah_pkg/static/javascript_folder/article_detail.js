document.addEventListener('DOMContentLoaded', function () {
  const articleBodies = document.querySelectorAll('[data-article-body]');

  function closestIgnoredElement(node) {
    return node.parentElement && node.parentElement.closest('script, style, pre, code');
  }

  function isWordCharacter(character) {
    return /[\p{L}\p{N}'’-]/u.test(character);
  }

  function completeWordEnd(text, startIndex, preferredLength) {
    let endIndex = startIndex + preferredLength;
    if (endIndex >= text.length) {
      return text.length;
    }

    if (isWordCharacter(text[endIndex]) && isWordCharacter(text[endIndex - 1])) {
      while (endIndex < text.length && isWordCharacter(text[endIndex])) {
        endIndex += 1;
      }
    }

    return endIndex;
  }

  function wrapOpeningText(root, maxChars) {
    if (!root || root.dataset.openingStyled === 'true') {
      return;
    }

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim() || closestIgnoredElement(node)) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });

    let remaining = maxChars;
    let firstLetterStyled = false;
    const nodes = [];

    while (remaining > 0) {
      const node = walker.nextNode();
      if (!node) {
        break;
      }
      nodes.push(node);
    }

    nodes.forEach((node) => {
      if (remaining <= 0) {
        return;
      }

      const text = node.nodeValue;
      const fragment = document.createDocumentFragment();
      let startIndex = 0;

      if (!firstLetterStyled) {
        const firstLetterIndex = text.search(/\S/);
        if (firstLetterIndex === -1) {
          return;
        }

        const dropCap = document.createElement('span');
        dropCap.className = 'article-dropcap';
        dropCap.textContent = text[firstLetterIndex];
        fragment.appendChild(dropCap);

        startIndex = firstLetterIndex + 1;
        firstLetterStyled = true;
        remaining -= 1;
      }

      const preferredLeadLength = Math.min(remaining, text.length - startIndex);
      const leadEndIndex = completeWordEnd(text, startIndex, preferredLeadLength);
      const leadText = text.slice(startIndex, leadEndIndex);
      if (leadText) {
        const leadSpan = document.createElement('span');
        leadSpan.className = 'article-lead-text';
        leadSpan.textContent = leadText;
        fragment.appendChild(leadSpan);
        remaining -= preferredLeadLength;
      }

      const restText = text.slice(leadEndIndex);
      if (restText) {
        fragment.appendChild(document.createTextNode(restText));
      }

      node.parentNode.replaceChild(fragment, node);
    });

    root.dataset.openingStyled = 'true';
  }

  function copyText(value, label) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value)
        .then(() => alertSuccess('Copied', `${label} copied successfully.`))
        .catch(() => fallbackCopyText(value, label));
      return;
    }
    fallbackCopyText(value, label);
  }

  function fallbackCopyText(value, label) {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    textarea.remove();
    alertSuccess('Copied', `${label} copied successfully.`);
  }

  articleBodies.forEach((body) => wrapOpeningText(body, 50));

  document.querySelectorAll('[data-share-article]').forEach((button) => {
    button.addEventListener('click', function () {
      const shareTitle = this.dataset.shareTitle || document.title;
      const shareUrl = this.dataset.shareUrl || window.location.href;

      if (navigator.share) {
        navigator.share({
          title: shareTitle,
          url: shareUrl
        }).catch(() => {});
        return;
      }

      copyText(shareUrl, 'Link');
    });
  });
});
