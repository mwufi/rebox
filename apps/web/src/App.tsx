import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, Route, Routes, useLocation, useNavigate, Link } from 'react-router-dom';
import { Icon, entityPath, Skel } from './components/bits';
import { Suggest } from './components/EntityPicker';
import { useTheme } from './lib/hooks';
import { HomePage } from './pages/Home';
import { SearchPage } from './pages/Search';
import { EntityPage } from './pages/Entity';
import { QueryPage } from './pages/Query';
import { ReviewPage } from './pages/Review';
import { ApiPage } from './pages/ApiDocs';
import { NotFound } from './pages/NotFound';
import { WorkerPage, WorkersPage } from './pages/Workers';

// d3-force is only needed on the graph page.
const GraphPage = lazy(() => import('./pages/Graph'));

function TopSearch() {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const loc = useLocation();

  useEffect(() => {
    setOpen(false);
  }, [loc.pathname]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) && !t.isContentEditable) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (loc.pathname === '/') return <div className="top-search" aria-hidden="true" style={{ visibility: 'hidden' }} />;

  return (
    <form
      className="top-search"
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        if (!q.trim()) return;
        setOpen(false);
        navigate(`/search?q=${encodeURIComponent(q.trim())}`);
      }}
    >
      <span className="glyph">
        <Icon name="search" size={13} />
      </span>
      <Suggest
        q={q}
        open={open}
        onClose={() => setOpen(false)}
        onPick={(h) => {
          setOpen(false);
          setQ('');
          navigate(entityPath(h.id));
        }}
      >
        {({ onKeyDown, listId, activeId, expanded }) => (
          <input
            ref={input}
            value={q}
            placeholder="Search people, companies…"
            aria-label="Search"
            role="combobox"
            aria-controls={listId}
            aria-expanded={expanded}
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            onChange={(e) => {
              setQ(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 120)}
            onKeyDown={onKeyDown}
          />
        )}
      </Suggest>
      <span className="kbd">/</span>
    </form>
  );
}

export function Layout() {
  const [theme, toggle] = useTheme();
  const dark =
    theme === 'dark' || (theme === 'system' && typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches);
  const loc = useLocation();
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [loc.pathname]);

  return (
    <>
      <a href="#main" className="sr-only">
        Skip to content
      </a>
      <header className="top">
        <div className="wrap">
          <Link to="/" className="brand" aria-label="Rebox home">
            <span className="brand-mark" aria-hidden="true" />
            Rebox
            <small>v0 · SF</small>
          </Link>
          <TopSearch />
          <nav className="nav" aria-label="Main">
            <NavLink to="/query">Query</NavLink>
            <NavLink to="/review" className="hide-sm">
              Review
            </NavLink>
            <NavLink to="/workers" className="hide-sm">
              Workers
            </NavLink>
            <NavLink to="/api">API</NavLink>
            <button type="button" className="icon-btn" onClick={toggle} aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'} title="Theme">
              <Icon name={dark ? 'sun' : 'moon'} />
            </button>
          </nav>
        </div>
      </header>
      <main id="main">
        <Outlet />
      </main>
      <footer className="foot">
        <div className="wrap">
          <span>Rebox — an open graph of people, companies and launches. Every fact carries its source.</span>
          <span>
            <Link to="/api">API</Link> · <Link to="/review">Review queue</Link>
          </span>
        </div>
      </footer>
    </>
  );
}

function GraphFallback() {
  return (
    <div className="wrap graph-page">
      <div className="graph-bar">
        <Skel w={240} h={24} />
      </div>
      <div className="graph-canvas" aria-busy="true" />
    </div>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="search" element={<SearchPage />} />
        <Route path="e/:id" element={<EntityPage />} />
        <Route
          path="e/:id/graph"
          element={
            <Suspense fallback={<GraphFallback />}>
              <GraphPage />
            </Suspense>
          }
        />
        <Route path="query" element={<QueryPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="workers" element={<WorkersPage />} />
        <Route path="workers/:id" element={<WorkerPage />} />
        <Route path="api" element={<ApiPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
