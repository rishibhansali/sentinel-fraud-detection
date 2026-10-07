import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './bootstrap.css'

function App() {
  return <main className="bootstrap"><h1>Sentinel</h1><p>Reviewer workspace</p></main>
}

createRoot(document.getElementById('root')).render(
  <StrictMode><App /></StrictMode>,
)
