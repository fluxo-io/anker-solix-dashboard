const channels = ['filter', 'data', 'snapshot'];
const levels = new Set(['success', 'info', 'warning', 'error']);

// Each Store holds at most one event. The acknowledgement is bounded to the
// three channels; returning to a healthy state permits a later new failure.
export function createNotificationConsumer(emit) {
  let delivery = 0;
  return (events, previous = {}) => {
    const acknowledged = {};
    let changed = false;
    channels.forEach((channel, index) => {
      const event = events[index];
      const valid = event && typeof event.eventId === 'string' &&
        event.eventId.length > 0 && event.eventId.length <= 128 &&
        typeof event.message === 'string' && event.message.trim().length > 0 &&
        event.message.length <= 1000 && levels.has(event.level);
      const oldId = previous && typeof previous[channel] === 'string' ? previous[channel] : null;
      const eventId = valid ? event.eventId : null;
      acknowledged[channel] = eventId;
      if (eventId !== oldId) {
        changed = true;
        if (valid) emit({level: event.level, message: event.message,
          eventId: `dash:${channel}:${eventId}:${++delivery}`});
      }
    });
    return changed ? acknowledged : null;
  };
}
