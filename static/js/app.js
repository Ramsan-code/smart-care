// Progressive enhancement: server-rendered forms work without JavaScript.
document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', () => {
    if (!form.checkValidity()) return;
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  });
});
document.querySelectorAll('.field-error').forEach((error) => {
  const input = error.parentElement.querySelector('input, select, textarea');
  if (input) { input.setAttribute('aria-invalid', 'true'); error.id = `${input.id}-error`; input.setAttribute('aria-describedby', error.id); }
});
