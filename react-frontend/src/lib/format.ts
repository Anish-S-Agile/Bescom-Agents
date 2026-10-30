/** Short en-US date/time, e.g. 10/6/26, 3:45 PM */
export function shortDate(d: Date): string {
  return d.toLocaleString('en-US', {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
    hour: 'numeric',
    minute: '2-digit',
  });
}
