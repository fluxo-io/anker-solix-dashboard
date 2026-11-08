import { notify, initNotifications } from '@fluxo/web-ui';
import '@fluxo/web-ui/styles.css';
import { createNotificationConsumer } from './notification-events.mjs';

const consume = createNotificationConsumer(notify);
window.dash_clientside = window.dash_clientside || {};
window.dash_clientside.fluxo = {
  ...window.dash_clientside.fluxo,
  consumeNotifications(filter, data, snapshot, acknowledged) {
    initNotifications();
    return consume([filter, data, snapshot], acknowledged) ?? window.dash_clientside.no_update;
  }
};

function start() {
  initNotifications();
  try {
    if (sessionStorage.getItem('solarbank.sessionExpired')) {
      sessionStorage.removeItem('solarbank.sessionExpired');
      notify({level: 'warning', message: 'Bitte erneut anmelden.'});
    }
  } catch (_) { /* Session feedback remains optional when storage is disabled. */ }
  let invalidPending = false;
  document.addEventListener('invalid', () => {
    if (invalidPending) return;
    invalidPending = true;
    queueMicrotask(() => { invalidPending = false; notify({level: 'error', message: 'Bitte prüfe die markierten Felder.'}); });
  }, true);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
else start();
