import './EmptyState.css';

// One empty-state for every admin list. Render it ONLY after a request has succeeded and returned zero records:
// callers keep their own loading and error branches, so a failed request is never shown as "No data found".
const EMPTY_MESSAGE = 'No data found';

export function EmptyState() {
  return <p className="empty empty-state" role="status">{EMPTY_MESSAGE}</p>;
}

export function EmptyRow({ colSpan }) {
  return <tr className="empty-state-row"><td colSpan={colSpan} className="empty empty-state">{EMPTY_MESSAGE}</td></tr>;
}
