import { Link } from 'react-router-dom';
import { useTitle } from '../lib/hooks';

export function NotFound({ what = 'There is nothing at this address.' }: { what?: string }) {
  useTitle('Not found');
  return (
    <div className="wrap" style={{ paddingTop: 64 }}>
      <span className="eyebrow">404</span>
      <h1 className="page-title" style={{ marginTop: 8 }}>
        Not in the graph
      </h1>
      <p className="lede" style={{ marginTop: 12 }}>
        {what} It may have been merged away or never existed. <Link to="/">Search from the start</Link>.
      </p>
    </div>
  );
}
