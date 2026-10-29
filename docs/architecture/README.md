# Architecture and delivery diagrams

Mermaid sources for the BenchNotes architecture and delivery diagrams. The published version, with rendered images, is the Confluence page [BenchNotes architecture and delivery](https://hmcts.atlassian.net/wiki/spaces/AIE/pages/365888217/BenchNotes+architecture+and+delivery).

Each `.mmd` file is the source. The block under each heading below is a copy of it, so GitHub renders the diagram here; update both together.

| File | Diagram |
|---|---|
| [`01-architecture.mmd`](01-architecture.mmd) | [Architecture](#architecture): The deployed components on CFT AKS, the HMCTS edge in front of them, the Azure resources they use and the services outside Azure. |
| [`02-use-cases.mmd`](02-use-cases.mmd) | [Use cases](#use-cases): What each kind of user can do in the dictation, hearing recordings and admin areas. |
| [`03-seq-sign-in.mmd`](03-seq-sign-in.mmd) | [Sign-in and API calls](#sign-in-and-api-calls): How transcribe-web signs a user in with Entra ID and how transcribe-api checks the token and roles on every call. |
| [`04-flow-recording.mmd`](04-flow-recording.mmd) | [Hearing recording flow](#hearing-recording-flow): A hearing recording from upload, through conversion and Azure Speech batch transcription, to review, including the failure paths. |
| [`05-seq-recording.mmd`](05-seq-recording.mmd) | [Hearing recording sequence](#hearing-recording-sequence): The same journey as calls between the browser, web, API, worker, Blob Storage, Postgres and Azure Speech. |
| [`06-seq-dictation-live.mmd`](06-seq-dictation-live.mmd) | [Live dictation](#live-dictation): Live transcription through Caddy to Azure Speech, draft autosave, and the Word decision document on submission. |
| [`07-seq-dictation-upload.mmd`](07-seq-dictation-upload.mmd) | [Dictation audio upload](#dictation-audio-upload): An uploaded audio file transcribed by the worker, with speaker names, minutes and a title from the LLM. |
| [`08-environments.mmd`](08-environments.mmd) | [Environments](#environments): Local, PR preview, demo, AAT and prod, and how code and images move between them. |
| [`09-branching.mmd`](09-branching.mmd) | [Branching](#branching): The develop, master, release, sync and demo branches and how changes flow between them. |
| [`10-pipeline.mmd`](10-pipeline.mmd) | [Build and deploy pipeline](#build-and-deploy-pipeline): GitHub checks, the Jenkins CNP PR and master builds, and Flux image automation. |

## Updating a diagram

1. Edit the `.mmd` file and copy it into the matching block below.
2. Render a PNG:

   ```sh
   npx -y @mermaid-js/mermaid-cli@11 -i <file>.mmd -o <file>.png -b white -s 2
   ```

3. The Confluence page shows the diagrams as PNG attachments. Upload the new PNG over the old attachment of the same name so the page shows the change. The PNGs are not kept in this repo.

## Architecture

The deployed components on CFT AKS, the HMCTS edge in front of them, the Azure resources they use and the services outside Azure.

Source: [`01-architecture.mmd`](01-architecture.mmd)

```mermaid
---
config:
  layout: elk
---
flowchart TB
  user["User's browser"]
  entra["Entra ID, HMCTS tenant<br/>app registration benchnotes-env<br/>app roles granted through groups"]

  subgraph edge["HMCTS edge, owned by PlatOps"]
    direction LR
    afd["Azure Front Door + WAF"] --> fw["Hub firewall"] --> agw["CFT App Gateway"]
  end

  subgraph aks["CFT AKS cluster, namespace transcribe. Two clusters per environment (00 and 01)"]
    ingress["Traefik ingress"]
    subgraph web["transcribe-web pod"]
      direction LR
      caddy["Caddy<br/>reverse proxy"] -->|"pages, /auth, /api/jobs, /api/upload, /api/audio"| next["Next.js<br/>pages, sign-in, sessions"]
    end
    subgraph backend["transcribe-api image"]
      direction LR
      api["API pods<br/>FastAPI"]
      worker["Worker pod<br/>background pollers<br/>cluster 00 only"]
    end
  end

  subgraph azure["Azure resources, created by Terraform"]
    direction LR
    redis[("Redis<br/>web sessions")]
    pg[("Postgres<br/>Flexible Server")]
    blob[("Blob Storage<br/>audio and documents")]
    speech["Azure Speech<br/>private endpoint"]
    openai["Azure OpenAI<br/>gpt-4o, private endpoint<br/>live summaries, fallback"]
    kv["Key Vault<br/>transcribe-env"]
    ai["Application Insights"]
  end

  subgraph external["Services outside Azure"]
    direction LR
    gemini["Google Vertex AI, Gemini<br/>minutes, titles, speaker names"]
    langfuse["Langfuse<br/>prompts and LLM traces"]
    notify["GOV.UK Notify<br/>email, when configured"]
  end

  user -->|HTTPS| afd
  agw --> ingress --> caddy
  user -. "sign-in redirect" .-> entra
  next -. "code exchange" .-> entra
  next --> redis
  caddy -->|"other /api/* with the user's token"| api
  next -->|"recordings: /api/v1 with the user's token"| api
  caddy -->|"live dictation audio (WebSocket)"| speech
  api -. "checks token signature and roles" .-> entra
  backend --> pg
  backend --> blob
  backend -->|"transcription"| speech
  backend -->|"LLM calls"| gemini
  backend -->|"LLM calls"| openai
  backend --> langfuse
  backend --> notify
  speech -->|"reads uploaded audio"| blob
  kv -. "secrets mounted as files (CSI)" .-> aks
  aks -. "logs, traces, metrics" .-> ai

  classDef ext fill:#eef2f7,stroke:#5b6b80,color:#172b4d
  classDef app fill:#e3fcef,stroke:#006644,color:#172b4d
  classDef data fill:#fff7d6,stroke:#b65c02,color:#172b4d
  classDef idp fill:#eae6ff,stroke:#403294,color:#172b4d
  class afd,fw,agw,ingress ext
  class caddy,next,api,worker app
  class redis,pg,blob,speech,openai,kv,ai data
  class entra idp
  class gemini,langfuse,notify ext
```

## Use cases

What each kind of user can do in the dictation, hearing recordings and admin areas.

Source: [`02-use-cases.mmd`](02-use-cases.mmd)

```mermaid
---
config:
  layout: elk
---
flowchart LR
  judge["Actor<br/>Any signed-in user<br/>Judge, Normal or an admin role"]
  ltm["Actor<br/>LegalTextManager"]
  sysadmin["Actor<br/>SystemAdministrator"]

  subgraph bn["BenchNotes"]
    direction TB
    signin(["Sign in with HMCTS account"])
    subgraph dict["Dictation area"]
      direction TB
      live(["Dictate with live transcription"])
      submit(["Submit hearing notes, get a Word decision document"])
      upload(["Upload an audio file for transcription and minutes"])
      minutes(["Review and edit AI-generated minutes"])
      mine(["View, tag, download and delete own transcriptions"])
    end
    subgraph rec["Hearing recordings area"]
      direction TB
      recup(["Upload a hearing recording"])
      review(["Review transcript, play audio, correct and accept segments"])
      wer(["Upload a reference transcript to measure accuracy"])
      recdel(["Delete a recording and its transcript"])
    end
    subgraph adm["Admin area"]
      direction TB
      content(["Edit document content: hearing types, legal frameworks, anonymity statuses"])
      viewcontent(["View document content"])
      users(["Manage users and roles"])
      alltx(["View all users' transcriptions and recordings"])
    end
  end

  judge --- signin
  judge --- live
  judge --- submit
  judge --- upload
  judge --- minutes
  judge --- mine
  judge --- recup
  judge --- review
  judge --- wer
  judge --- recdel
  ltm --- signin
  ltm --- content
  ltm --- viewcontent
  sysadmin --- signin
  sysadmin --- viewcontent
  sysadmin --- users
  sysadmin --- alltx

  classDef actor fill:#eae6ff,stroke:#403294,color:#172b4d
  classDef uc fill:#ffffff,stroke:#5b6b80,color:#172b4d
  class judge,ltm,sysadmin actor
  class signin,live,submit,upload,minutes,mine,recup,review,wer,recdel,content,viewcontent,users,alltx uc
```

## Sign-in and API calls

How transcribe-web signs a user in with Entra ID and how transcribe-api checks the token and roles on every call.

Source: [`03-seq-sign-in.mmd`](03-seq-sign-in.mmd)

```mermaid
sequenceDiagram
  autonumber
  actor U as User's browser
  participant W as transcribe-web<br/>(Caddy + Next.js)
  participant R as Redis
  participant E as Entra ID
  participant A as transcribe-api
  participant D as Postgres

  U->>W: Open any page
  W-->>U: No session cookie, redirect to /auth/login
  U->>W: GET /auth/login
  W->>R: Save pending login (state, PKCE verifier), 10 minutes
  W-->>U: Redirect to Entra sign-in
  U->>E: Sign in with HMCTS account
  E-->>U: Redirect to /auth/callback with a code
  U->>W: GET /auth/callback
  W->>R: Take the pending login (single use)
  W->>E: Exchange code for tokens
  E-->>W: ID token with app roles, refresh token
  alt user holds none of the access roles
    W-->>U: 403 "You do not have access to BenchNotes"
  else user holds a role
    W->>R: Store session (tokens, roles)
    W-->>U: Set httpOnly session cookie, redirect to the page
  end

  Note over U,A: Every later API call
  U->>W: /api/... with session cookie
  W->>W: Caddy drops any Authorization header,<br/>asks Next.js for the session's token<br/>(refreshed with Entra when needed)
  W->>A: Request with Authorization: Bearer ID token
  A->>E: Fetch signing keys (cached)
  A->>A: Verify signature, audience, issuer, expiry.<br/>Check the user holds an access role
  A->>D: Find or create the user, sync role from token
  A->>A: Check the route's required role
  alt role allowed
    A-->>W: 200 with data
  else not allowed
    A-->>W: 401 or 403
  end
  W-->>U: Response
```

## Hearing recording flow

A hearing recording from upload, through conversion and Azure Speech batch transcription, to review, including the failure paths.

Source: [`04-flow-recording.mmd`](04-flow-recording.mmd)

```mermaid
---
config:
  layout: elk
---
flowchart TB
  start(["User chooses one or more recordings<br/>on the Hearing recordings page"])
  up["Web sends each file to the API<br/>POST /api/v1/uploads"]
  check{"Allowed type<br/>and under 200 MB?"}
  conv["API converts it to 16 kHz mono WAV<br/>with ffmpeg"]
  decode{"Audio<br/>decodable?"}
  store[("Blob Storage<br/>uploads/user/id.wav")]
  job["Web creates a job<br/>POST /api/v1/jobs"]
  own{"File belongs<br/>to this user?"}
  submit["API saves the job (pending) and submits<br/>an Azure Speech batch transcription"]
  accepted{"Speech<br/>accepted it?"}
  queued["Job status: submitted"]
  speech["Azure Speech fetches the audio from Blob Storage<br/>and transcribes it"]
  poll["Worker checks every pending job<br/>every 30 seconds"]
  status{"Speech job<br/>status"}
  running["Job status: running"]
  ok["Worker downloads the result, labels speakers,<br/>saves the transcript in Postgres,<br/>deletes the Speech job"]
  fail["Job status: failed"]
  view(["User opens the job: reads the transcript,<br/>plays the audio, corrects and accepts segments"])
  reject(["Rejected with an error message"])
  unavailable(["Job failed, user sees 'service unavailable'"])

  start --> up --> check
  check -- no --> reject
  check -- yes --> conv --> decode
  decode -- no --> reject
  decode -- yes --> store --> job --> own
  own -- no --> reject
  own -- yes --> submit --> accepted
  accepted -- no --> unavailable
  accepted -- yes --> queued --> speech
  speech --> poll --> status
  status -- running --> running --> poll
  status -- not started --> poll
  status -- succeeded --> ok --> view
  status -- failed --> fail

  classDef user fill:#eae6ff,stroke:#403294,color:#172b4d
  classDef step fill:#e3fcef,stroke:#006644,color:#172b4d
  classDef bad fill:#ffebe6,stroke:#bf2600,color:#172b4d
  classDef ext fill:#fff7d6,stroke:#b65c02,color:#172b4d
  class start,view user
  class up,conv,job,submit,queued,poll,running,ok step
  class reject,unavailable,fail bad
  class speech,store ext
```

## Hearing recording sequence

The same journey as calls between the browser, web, API, worker, Blob Storage, Postgres and Azure Speech.

Source: [`05-seq-recording.mmd`](05-seq-recording.mmd)

```mermaid
sequenceDiagram
  autonumber
  actor U as User's browser
  participant W as transcribe-web
  participant A as transcribe-api
  participant B as Blob Storage
  participant D as Postgres
  participant S as Azure Speech
  participant K as API worker

  U->>W: Upload recording (Hearing recordings page)
  W->>A: POST /api/v1/uploads with the user's token
  A->>A: Check type and size, convert to WAV (ffmpeg)
  A->>B: Store uploads/user/id.wav
  A-->>W: Blob name and URL
  W->>A: POST /api/v1/jobs
  A->>A: Check the blob belongs to this user
  A->>D: Save job (pending)
  A->>S: Create batch transcription for the blob URL
  S-->>A: Speech job reference
  A->>D: Job submitted
  A-->>W: Job
  W-->>U: Job listed as in progress

  S->>B: Read the audio (Speech's own identity)
  loop every 30 seconds
    K->>D: Find unfinished jobs
    K->>S: Get job status
    S-->>K: NotStarted, Running, Succeeded or Failed
    K->>D: Update status
  end
  K->>S: Download results (on Succeeded)
  K->>K: Label speakers, read model and duration
  K->>D: Save transcript segments, job succeeded
  K->>S: Delete the Speech job

  loop while the page is open
    U->>W: Poll the job list or job
    W->>A: GET /api/v1/jobs
    A->>D: Read jobs (own jobs, or all for SystemAdministrator)
    A-->>U: Status and transcript
  end
  U->>A: Play audio (GET /api/v1/jobs/id/audio, via web)
  A->>B: Stream the WAV
  U->>A: Correct or accept a segment (PATCH or POST, via web)
  A->>D: Save the edit and its history
```

## Live dictation

Live transcription through Caddy to Azure Speech, draft autosave, and the Word decision document on submission.

Source: [`06-seq-dictation-live.mmd`](06-seq-dictation-live.mmd)

```mermaid
sequenceDiagram
  autonumber
  actor U as User's browser
  participant W as transcribe-web<br/>(Caddy)
  participant A as transcribe-api
  participant I as Entra ID<br/>(workload identity)
  participant S as Azure Speech<br/>(private endpoint)
  participant D as Postgres
  participant B as Blob Storage

  U->>W: Start dictation (Dictation page)
  W->>A: GET /api/get-speech-token with the user's token
  A->>I: Get a token for Cognitive Services as the pod's managed identity
  I-->>A: Entra token
  A-->>U: Short-lived Speech token, endpoint = BenchNotes' own address
  U->>U: Microphone permission
  U->>W: Open WebSocket /cognitiveservices/... with the Speech token
  W->>S: Caddy forwards it over the private endpoint
  loop while dictating
    U->>S: Audio stream (through Caddy)
    S-->>U: Interim and final text (en-GB)
    U->>A: PUT /api/live-draft (autosave, via web)
    A->>D: Save the in-progress draft
  end

  U->>A: Submit hearing: POST /api/live-transcription/submission
  A->>B: Save the submission (form and transcript) as JSON
  A->>D: Load document content (hearing types, legal frameworks, anonymity)
  A->>A: Render the Word decision document
  A->>B: Store the .docx
  A->>D: Save the transcription record
  A-->>U: Download link (/api/download)
  U->>A: GET /api/download (via web)
  A->>D: Record the export in the audit log
  A->>B: Read the .docx
  A-->>U: Word document
```

## Dictation audio upload

An uploaded audio file transcribed by the worker, with speaker names, minutes and a title from the LLM.

Source: [`07-seq-dictation-upload.mmd`](07-seq-dictation-upload.mmd)

```mermaid
sequenceDiagram
  autonumber
  actor U as User's browser
  participant A as transcribe-api<br/>(via web)
  participant B as Blob Storage
  participant K as API worker
  participant S as Azure Speech
  participant L as LLM<br/>(Gemini on Vertex AI,<br/>Azure fallback)
  participant D as Postgres

  U->>A: POST /api/get-upload-url (file type)
  A-->>U: Upload link with a new transcription ID
  U->>A: PUT /api/upload-audio (the file)
  A->>A: Check the key names this user's own upload folder
  A->>B: Store user-uploads/user/id.ext

  loop every 30 seconds
    K->>B: List new files under user-uploads/
  end
  K->>B: Claim the file (metadata: in progress)
  K->>D: Find the owning user
  K->>B: Download the audio
  K->>S: Fast transcription of the audio
  S-->>K: Transcript with speaker labels
  K->>L: Predict speaker names
  K->>D: Save transcription and transcript
  par minutes and title
    K->>L: Generate minutes (two templates)
  and
    K->>L: Generate a title
  end
  K->>D: Save minutes and title
  K->>B: Store a .docx of the transcript
  K->>K: Email the user (only if GOV.UK Notify is configured)
  K->>B: Mark processed, delete the audio

  U->>A: Open the transcription, view minutes
  U->>A: Ask for edited minutes (POST /api/generate-or-edit-minutes)
  A->>L: Edit minutes with the user's instructions
  A->>D: Save a new minutes version
  U->>A: Poll for the new version, then download
```

## Environments

Local, PR preview, demo, AAT and prod, and how code and images move between them.

Source: [`08-environments.mmd`](08-environments.mmd)

```mermaid
---
config:
  layout: elk
---
flowchart LR
  local["Local<br/>developer machine<br/>Docker Postgres + Azurite"]

  subgraph pr["Every pull request"]
    preview["PR preview<br/>CFT preview cluster<br/>own Postgres, Azurite, mock Speech<br/>sign-in off"]
  end

  subgraph dev["develop branch"]
    draft["Permanent draft PR<br/>api#45, web#25<br/>image pr-N-sha-time"]
  end

  subgraph main["master branch"]
    staging["Jenkins staging release in AAT<br/>smoke and functional tests"]
    tag["Image promoted to<br/>prod-sha-time"]
  end

  demo["Demo<br/>benchnotes.demo.platform.hmcts.net<br/>clusters demo-00, demo-01"]
  aat["AAT<br/>benchnotes.aat.platform.hmcts.net<br/>clusters aat-00, aat-01"]
  prod["Prod<br/>benchnotes.platform.hmcts.net<br/>clusters prod-00, prod-01"]

  local -->|"open a PR"| preview
  preview -->|"feature PR merged"| draft
  draft -->|"Flux follows pr-N tags"| demo
  preview -->|"release PR merged"| staging
  staging -->|"tests pass"| tag
  tag -->|"Flux follows prod tags"| aat
  tag -->|"Flux follows prod tags"| prod

  classDef env fill:#e3fcef,stroke:#006644,color:#172b4d
  classDef live fill:#ffebe6,stroke:#bf2600,color:#172b4d
  classDef step fill:#eef2f7,stroke:#5b6b80,color:#172b4d
  class local,preview,demo,aat env
  class prod live
  class draft,staging,tag step
```

## Branching

The develop, master, release, sync and demo branches and how changes flow between them.

Source: [`09-branching.mmd`](09-branching.mmd)

```mermaid
---
config:
  gitGraph:
    mainBranchName: master
    showCommitLabel: true
    rotateCommitLabel: true
---
gitGraph
  commit id: "release 1"
  branch demo order: 5
  commit id: "copy of master"
  checkout master
  branch develop order: 1
  commit id: "develop"
  branch feature/DIAAT-301 order: 2
  commit id: "work"
  commit id: "more work"
  checkout develop
  merge feature/DIAAT-301 id: "feature PR" tag: "demo"
  branch feature/DIAAT-302 order: 3
  commit id: "work 2"
  checkout develop
  merge feature/DIAAT-302 id: "feature PR 2" tag: "demo"
  checkout master
  commit id: "migration PR"
  branch sync/master-to-develop order: 4
  commit id: "cut from master"
  checkout develop
  merge sync/master-to-develop id: "master merged in"
  branch release/DIAAT-301 order: 6
  commit id: "cut from develop"
  checkout master
  merge release/DIAAT-301 id: "release PR, squashed" tag: "AAT + prod"
  checkout demo
  merge master id: "force-synced by Jenkins"
```

## Build and deploy pipeline

GitHub checks, the Jenkins CNP PR and master builds, and Flux image automation.

Source: [`10-pipeline.mmd`](10-pipeline.mmd)

```mermaid
---
config:
  layout: elk
---
flowchart TB
  dev(["Developer opens a PR<br/>(Conventional Commit title with Jira key)"])

  subgraph gh["GitHub checks on the PR"]
    direction LR
    ruff["GitHub Actions: Ruff lint<br/>(transcribe-api, required on master)"]
    copilot["Copilot review"]
  end

  subgraph prjob["Jenkins CNP pipeline on build.hmcts.net: PR build"]
    direction TB
    build["Checkout, Build<br/>(uv sync, or yarn install + lint)"]
    par["Static checks / Container build, in parallel:<br/>unit tests + Sonar scan, security checks (uv or yarn audit),<br/>tech stack check, Docker image build in ACR hmctsprod"]
    prtag["Promote Docker Image: tag pr-N-sha-time"]
    tfplan["Terraform plan for AAT and prod<br/>(only when infrastructure/ changed)"]
    prdeploy["AKS deploy - preview, then smoke and functional tests"]
    build --> par --> prtag --> tfplan --> prdeploy
  end

  review(["Review, checks green, merge.<br/>develop: merge or squash. master: squash only"])

  subgraph mjob["Jenkins CNP pipeline: master build"]
    direction TB
    mbuild["Same build, tests, scans and image build"]
    tfaat["Terraform apply: AAT"]
    staging["AKS deploy - AAT staging release,<br/>smoke and functional tests"]
    chart["Publish Helm chart to hmcts-charts"]
    tfprod["Terraform apply: prod<br/>(repo listed in cnp-jenkins-config environment approvals)"]
    prodtag["Prod build promotion: tag prod-sha-time"]
    sync["Sync branches with master:<br/>force-push demo, ithc, perftest"]
    mbuild --> tfaat --> staging --> chart --> tfprod --> prodtag --> sync
  end

  subgraph flux["Flux in cnp-flux-config"]
    direction TB
    scan["ImageRepository scans hmctsprod"]
    policy["ImagePolicy picks the newest tag:<br/>prod-* for AAT and prod,<br/>pr-45-* and pr-25-* for demo"]
    commit["Image automation commits the new tag<br/>to cnp-flux-config master"]
    helm["HelmRelease upgrade on both clusters<br/>(chart from hmcts-charts)"]
    scan --> policy --> commit --> helm
  end

  demoTf["demo branch build: Terraform apply for demo only"]
  envs(["Running in AAT, prod (from master)<br/>or demo (from develop)"])

  dev --> gh
  dev --> prjob
  gh --> review
  prjob --> review
  review -->|"into develop: draft PR #45/#25 rebuilds"| prjob
  review -->|"into master"| mjob
  prtag -.->|"pr-N image"| scan
  prodtag -.->|"prod image"| scan
  sync -.-> demoTf
  helm --> envs

  classDef gh fill:#eef2f7,stroke:#5b6b80,color:#172b4d
  classDef jen fill:#e3fcef,stroke:#006644,color:#172b4d
  classDef fx fill:#eae6ff,stroke:#403294,color:#172b4d
  classDef person fill:#fff7d6,stroke:#b65c02,color:#172b4d
  class ruff,copilot gh
  class build,par,prtag,tfplan,prdeploy,mbuild,tfaat,staging,chart,tfprod,prodtag,sync,demoTf jen
  class scan,policy,commit,helm fx
  class dev,review,envs person
```
