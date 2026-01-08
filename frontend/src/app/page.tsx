'use client'

import { useState, useRef, useEffect } from 'react'

type Agent = {
  id: string
  name: string
  description: string
}

function parseAgentsFromResponse(text: string): Agent[] {
  const agents: Agent[] = []
  
  // Pattern 1: emoji **Name** - Description
  const pattern1 = /[📄🚢⚓📋]\s*\*\*([^*]+)\*\*\s*[-–]\s*([^\n]+)/g
  
  // Pattern 2: **emoji Name** followed by bullet points
  const pattern2 = /\*\*\s*[📄🚢⚓📋]\s*([^*]+)\*\*/g
  
  let match
  
  // Try pattern 1 first
  while ((match = pattern1.exec(text)) !== null) {
    agents.push({
      id: match[1].trim().toLowerCase().replace(/\s+/g, '-'),
      name: match[1].trim(),
      description: match[2].trim()
    })
  }
  
  // If no matches, try pattern 2
  if (agents.length === 0) {
    while ((match = pattern2.exec(text)) !== null) {
      const name = match[1].trim()
      
      // Get bullet points after this heading as description
      const afterMatch = text.slice(match.index + match[0].length)
      const bullets = afterMatch.match(/^[\s\S]*?(?=\*\*|$)/)?.[0] || ''
      const firstBullet = bullets.match(/-\s*([^\n]+)/)?.[1]?.trim() || ''
      
      agents.push({
        id: name.toLowerCase().replace(/\s+/g, '-'),
        name,
        description: firstBullet
      })
    }
  }
  
  return agents
}

export default function Home() {
  const [messages, setMessages] = useState<{role: string, content: string}[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [sessionId] = useState(`session-${Date.now()}`)
  const bottomRef = useRef<HTMLDivElement>(null)
  
  const [agents, setAgents] = useState<Agent[]>([])
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const send = async () => {
    if (!input.trim() || loading) return
    const msg = input.trim()
    setInput('')
    setMessages(m => [...m, { role: 'user', content: msg }])
    setLoading(true)

    try {
      const res = await fetch(process.env.NEXT_PUBLIC_API_URL!, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-api-key': process.env.NEXT_PUBLIC_API_KEY! },
        body: JSON.stringify({ prompt: msg, session_id: sessionId })
      })
      const data = await res.json()
      const content = data.response || data.error || 'No response'
      setMessages(m => [...m, { role: 'assistant', content }])
      
      // Parse agents from response if panel is empty
      if (agents.length === 0) {
        const parsed = parseAgentsFromResponse(content)
        if (parsed.length > 0) {
          setAgents(parsed)
        }
      }
    } catch (e: any) {
      setMessages(m => [...m, { role: 'error', content: e.message }])
    } finally {
      setLoading(false)
    }
  }

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    
    setUploading(true)
    setMessages(m => [...m, { role: 'user', content: `📎 ${file.name}` }])
    
    try {
      // Get presigned URL
      const res = await fetch('/api/upload-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, contentType: file.type })
      })
      
      if (!res.ok) {
        throw new Error('Failed to get upload URL')
      }
      
      const { uploadUrl, s3Path } = await res.json()
      
      // Upload to S3
      const uploadRes = await fetch(uploadUrl, { 
        method: 'PUT', 
        body: file,
        headers: { 'Content-Type': file.type }
      })
      
      if (!uploadRes.ok) {
        throw new Error('Failed to upload file to S3')
      }
      
      // Show processing message
      setLoading(true)
      
      // Call hub orchestrator with extended timeout using AbortController
      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), 120000) // 2 minute timeout
      
      try {
        const hubRes = await fetch(process.env.NEXT_PUBLIC_API_URL!, {
          method: 'POST',
          headers: { 
            'Content-Type': 'application/json', 
            'x-api-key': process.env.NEXT_PUBLIC_API_KEY! 
          },
          body: JSON.stringify({ 
            prompt: `Extract data from this S3 URI (${s3Path})`, 
            session_id: sessionId 
          }),
          signal: controller.signal
        })
        
        clearTimeout(timeoutId)
        
        if (!hubRes.ok) {
          // Check for API Gateway timeout
          if (hubRes.status === 504) {
            setMessages(m => [...m, { 
              role: 'assistant', 
              content: '⏳ Document uploaded successfully! Processing is taking longer than expected. The document is being processed in the background - please try asking about it in a moment, or check CloudWatch logs for the result.' 
            }])
            return
          }
          const errorText = await hubRes.text()
          throw new Error(`API error: ${hubRes.status} - ${errorText}`)
        }
        
        const data = await hubRes.json()
        setMessages(m => [...m, { role: 'assistant', content: data.response || data.error || 'Processing complete but no response data' }])
      } catch (fetchError: unknown) {
        clearTimeout(timeoutId)
        if (fetchError instanceof Error && fetchError.name === 'AbortError') {
          throw new Error('Request timed out - document processing is taking too long')
        }
        throw fetchError
      }
    } catch (err: unknown) {
      const errorMessage = err instanceof Error ? err.message : 'Upload failed'
      setMessages(m => [...m, { role: 'error', content: errorMessage }])
    } finally {
      setUploading(false)
      setLoading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  return (
    <div className="container">
      <header>
        <img src="/CrowleyLogo.png" alt="Crowley Logo" className="logo" />
        <h1>🚢 Crowley Agentic Platform</h1>
      </header>
      
      <div className="content-wrapper">
        <aside className="agents-panel">
          <h2>Available Agents</h2>
          {agents.length === 0 ? (
            <div className="agents-empty">Send a message to discover agents</div>
          ) : (
            agents.map(agent => (
              <div key={agent.id} className="agent-item">
                <div className="agent-header">
                  <span className="status-dot healthy" />
                  <span className="agent-name">{agent.name}</span>
                </div>
                <p className="agent-description">{agent.description}</p>
              </div>
            ))
          )}
          <div className="upload-section">
            <input type="file" ref={fileInputRef} onChange={handleFileUpload} accept=".pdf,.png,.jpg,.jpeg,.docx,.xlsx" hidden />
            <button className="upload-btn" onClick={() => fileInputRef.current?.click()} disabled={uploading}>
            {uploading ? 'Uploading...' : '📄 Upload Document'}
            </button>
          </div>
        </aside>
        <div className="chat-section">
          <main>
            {messages.map((m, i) => <div key={i} className={`msg ${m.role}`}>{m.content}</div>)}
            {loading && <div className="msg assistant">Thinking...</div>}
            <div ref={bottomRef} />
          </main>
          
          <footer>
            <input 
              value={input} 
              onChange={e => setInput(e.target.value)} 
              onKeyDown={e => e.key === 'Enter' && send()}
              placeholder="Type your message..." 
            />
            <button onClick={send} disabled={loading || !input.trim()}>Send</button>
          </footer>
        </div>
      </div>
    </div>
  )
}