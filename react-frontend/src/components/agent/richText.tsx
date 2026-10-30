import { Fragment, type ReactNode } from 'react';

/**
 * Tiny markdown subset for orchestrator replies: **bold**, *italic*, `code`, "> quote" blocks, line breaks.
 * Everything is rendered as React text nodes, never as HTML, so model output cannot inject markup.
 */
const INLINE = /(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`)/g;

function inline(text: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}

export function RichText({ text }: { text: string }) {
  const blocks: { quote: boolean; lines: string[] }[] = [];
  for (const raw of (text ?? '').split('\n')) {
    const quote = raw.startsWith('>');
    const line = quote ? raw.replace(/^>\s?/, '') : raw;
    const last = blocks[blocks.length - 1];
    if (last && last.quote === quote) last.lines.push(line);
    else blocks.push({ quote, lines: [line] });
  }
  return (
    <div className="rich-text">
      {blocks.map((b, i) => {
        const content = b.lines.map((l, j) => (
          <Fragment key={j}>
            {j > 0 && <br />}
            {inline(l)}
          </Fragment>
        ));
        return b.quote ? <blockquote key={i}>{content}</blockquote> : <p key={i}>{content}</p>;
      })}
    </div>
  );
}
