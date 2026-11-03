// Compatibility for handoff links created before the SPA route was introduced.
const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
history.replaceState(null, '', '/me/');
location.replace('/#/me' + (ticket ? '?ticket=' + encodeURIComponent(ticket) : ''));
