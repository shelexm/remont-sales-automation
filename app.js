(() => {
  const form = document.querySelector('#demo-form');
  const status = document.querySelector('#form-status');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    if (!window.DEMO_API_URL) {
      status.textContent = 'Демо-приёмник пока не подключён. Попробуйте позже.';
      return;
    }
    const button = form.querySelector('button');
    button.disabled = true;
    status.textContent = 'Отправляем тестовую заявку…';
    try {
      const response = await fetch(`${window.DEMO_API_URL}/demo/lead`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams(new FormData(form)),
        signal: AbortSignal.timeout(8000),
      });
      if (response.status === 429) {
        status.textContent = 'Лимит демонстрационных заявок временно исчерпан. Попробуйте позже.';
      } else if (!response.ok) {
        status.textContent = 'Приёмник временно недоступен. Попробуйте позже.';
      } else {
        status.textContent = 'Тестовая заявка принята. Создание сделки и уведомление выполняются отдельно.';
        form.reset();
      }
    } catch {
      status.textContent = 'Не удалось связаться с приёмником. Проверьте доступность демостенда.';
    } finally {
      button.disabled = false;
    }
  });
})();
