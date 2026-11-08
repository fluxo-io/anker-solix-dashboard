import { notify, initNotifications } from '@fluxo/web-ui';
import '@fluxo/web-ui/styles.css';
import { createNotificationConsumer } from './notification-events.mjs';

let observedHeader;
let headerObserver;
function positionNotifications() {
  const header = document.querySelector('.app-header, .public-header');
  if (header !== observedHeader) {
    headerObserver?.disconnect();
    observedHeader = header;
    if (header && typeof ResizeObserver !== 'undefined') {
      headerObserver = new ResizeObserver(positionNotifications);
      headerObserver.observe(header);
    }
  }
  const bottom = Math.max(0, header?.getBoundingClientRect().bottom || 0);
  document.body.style.setProperty('--notification-top', `${bottom + 16}px`);
}
window.addEventListener('resize', positionNotifications);
window.addEventListener('scroll', positionNotifications, {passive: true});

const consume = createNotificationConsumer(notify);
window.dash_clientside = window.dash_clientside || {};
window.dash_clientside.fluxo = {
  ...window.dash_clientside.fluxo,
  consumeNotifications(filter, data, snapshot, acknowledged) {
    positionNotifications();
    initNotifications();
    return consume([filter, data, snapshot], acknowledged) ?? window.dash_clientside.no_update;
  }
};

function start() {
  positionNotifications();
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
    // Native validation can checkpoint microtasks between invalid fields.
    window.setTimeout(() => {
      invalidPending = false;
      notify({level: 'error', message: 'Bitte prüfe die markierten Felder.'});
    }, 0);
  }, true);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
else start();
