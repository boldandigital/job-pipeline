/**
 * CV Generator v2
 * Configurable section order, tighter spacing, full side projects
 *
 * IMPORTANT: Personal data (name, email, phone, location) should be configured
 * via environment variables or a separate config file. The placeholder values
 * below are examples only.
 *
 * Environment variables:
 *   CV_NAME, CV_EMAIL, CV_PHONE, CV_LOCATION, CV_PHOTO_PATH
 *
 * Usage: node cv_template_v2.js --company "TechCorp" --tagline "AI Specialist"
 *        --language en --order "experience,skills,projects,education"
 *        --output /path/to/CV.docx
 */

const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, ImageRun,
        AlignmentType, WidthType, BorderStyle, VerticalAlign, LevelFormat } = require('docx');
const fs = require('fs');
const path = require('path');

// ============================================================================
// CLI ARGS
// ============================================================================
const args = process.argv.slice(2);
function getArg(name, fallback) {
  const idx = args.indexOf("--" + name);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : fallback;
}

const COMPANY = getArg("company", "COMPANY");
const TAGLINE = getArg("tagline", "Business Informatics | AI & Digital Innovation");
const LANG = getArg("language", "en");
const ORDER = getArg("order", "experience,skills,projects,education").split(",").map(s => s.trim());
const OUTPUT = getArg("output", `CV_Applicant_${COMPANY}.docx`);
// Optional: comma-separated list of experience keys to include
const EXP_FILTER = getArg("experiences", "").split(",").map(s => s.trim()).filter(Boolean);
// Optional: comma-separated list of project keys to include
const PROJ_FILTER = getArg("projects", "").split(",").map(s => s.trim()).filter(Boolean);

console.log(`Generating CV v2: company=${COMPANY}, lang=${LANG}, order=${ORDER.join(",")}`);
console.log(`Output: ${OUTPUT}`);

// ============================================================================
// PERSONAL DATA (configure via env vars or config file)
// ============================================================================
const PERSONAL = {
  name: process.env.CV_NAME || 'JANE DOE',
  email: process.env.CV_EMAIL || 'jane.doe@example.com',
  phone: process.env.CV_PHONE || '+49 123 4567890',
  location: process.env.CV_LOCATION || 'Berlin',
};

// ============================================================================
// STYLING - tighter spacing
// ============================================================================
const noBorder = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };
const sectionLine = { style: BorderStyle.SINGLE, size: 6, color: "000000" };

const scriptDir = path.dirname(process.argv[1]) || '.';
const photoPath = process.env.CV_PHOTO_PATH || path.join(scriptDir, 'photo.png');
const photoData = fs.existsSync(photoPath) ? fs.readFileSync(photoPath) : null;

const FONT = "DejaVu Serif";
const A4_WIDTH = 11906;
const A4_HEIGHT = 16838;
const MARGIN = 680;
const CONTENT_WIDTH = A4_WIDTH - 2 * MARGIN;
const DATE_COL = 1700;
const MAIN_COL = CONTENT_WIDTH - DATE_COL;

// ============================================================================
// HELPERS
// ============================================================================
function sectionHeading(text) {
  return new Paragraph({
    spacing: { before: 200, after: 60 },
    border: { bottom: sectionLine },
    children: [new TextRun({ text: text.toUpperCase(), bold: true, size: 22, font: FONT })]
  });
}

function entryHeader(period, titleLine) {
  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [DATE_COL, MAIN_COL],
    rows: [new TableRow({
      children: [
        new TableCell({
          borders: noBorders, width: { size: DATE_COL, type: WidthType.DXA },
          verticalAlign: VerticalAlign.TOP,
          children: [new Paragraph({ spacing: { after: 20 }, children: [
            new TextRun({ text: period, size: 19, font: FONT, color: "555555" })
          ]})]
        }),
        new TableCell({
          borders: noBorders, width: { size: MAIN_COL, type: WidthType.DXA },
          verticalAlign: VerticalAlign.TOP,
          children: [new Paragraph({ spacing: { after: 20 }, children: [
            new TextRun({ text: titleLine, bold: true, size: 20, font: FONT })
          ]})]
        })
      ]
    })]
  });
}

function bullet(text) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 30 },
    indent: { left: DATE_COL + 300, hanging: 300 },
    children: [new TextRun({ text, size: 19, font: FONT })]
  });
}

function skillRow(label, value) {
  return new TableRow({
    children: [
      new TableCell({
        borders: noBorders, width: { size: 2200, type: WidthType.DXA },
        children: [new Paragraph({ spacing: { after: 40 }, children: [
          new TextRun({ text: label, bold: true, size: 19, font: FONT })
        ]})]
      }),
      new TableCell({
        borders: noBorders, width: { size: CONTENT_WIDTH - 2200, type: WidthType.DXA },
        children: [new Paragraph({ spacing: { after: 40 }, children: [
          new TextRun({ text: value, size: 19, font: FONT })
        ]})]
      })
    ]
  });
}

// ============================================================================
// ALL CONTENT DATA (example entries -- replace with your own)
// ============================================================================
const ALL_EXPERIENCE = {
  techcorp: {
    period: '11/2024 –\n09/2025',
    title_en: 'TechCorp GmbH, Munich (Remote) – Working Student, Digital Innovation',
    title_de: 'TechCorp GmbH, Muenchen (Remote) – Werkstudent, Digital Innovation',
    bullets_en: [
      'Built AI-powered research automation pipelines using Python and Claude API, reducing report creation time by 60%',
      'Co-designed regulatory-compliant infrastructure models across Finance, IT, and Compliance departments',
      'Led independent vendor evaluations for integrating blockchain and RegTech solutions into enterprise architecture',
      'Produced weekly market reports for institutional clients covering 50+ digital assets',
    ],
    bullets_de: [
      'Aufbau AI-gestuetzter Research-Pipelines (Python, Claude API) -- 60% schnellere Report-Erstellung',
      'Co-Design regulatorischer Infrastrukturmodelle (MiCAR, DORA, ISO 27001)',
      'Eigenstaendige Vendor-Evaluierungen fuer Blockchain/RegTech-Integration',
      'Woechentliche Market Reports fuer institutionelle Kunden (50+ Digital Assets)',
    ]
  },
  startupxyz: {
    period: '09/2023 –\n02/2024',
    title_en: 'StartupXYZ, Frankfurt – Intern, Digital Assets & Innovation',
    title_de: 'StartupXYZ, Frankfurt – Praktikant, Digital Assets & Innovation',
    bullets_en: [
      'Developed AI-based investment framework using XGBoost for tokenized asset valuation on multivariate market data',
      'Conducted data analysis and automated reporting using Excel, SQL, and Power BI',
      'Documented and optimized internal business processes for digital transformation initiatives',
    ],
    bullets_de: [
      'Entwicklung AI-basiertes Investment-Framework (XGBoost) fuer tokenisierte Asset-Bewertung',
      'Datenanalyse und automatisiertes Reporting (Excel, SQL, Power BI)',
      'Prozessdokumentation und -optimierung fuer Digitalisierungsinitiativen',
    ]
  },
  medcenter: {
    period: '08/2021 –\n12/2022',
    title_en: 'University Medical Center – Working Student, Clinical IT',
    title_de: 'Universitaetsmedizin – Werkstudent, Klinische IT',
    bullets_en: [
      'Digitized medical care processes through IT-supported process modeling and data flow analysis',
      'Planned and scaled interdisciplinary training programs supporting system rollouts',
    ],
    bullets_de: [
      'Digitalisierung medizinischer Versorgungsprozesse durch IT-gestuetzte Prozessmodellierung',
      'Planung und Skalierung interdisziplinaerer Schulungsprogramme fuer System-Rollouts',
    ]
  },
  industryco: {
    period: '09/2019 –\n07/2021',
    title_en: 'IndustryCo AG – Working Student, Process Automation',
    title_de: 'IndustryCo AG – Werkstudent, Prozessautomatisierung',
    bullets_en: [
      'End-to-end automation of business processes using UiPath and Python, eliminating manual interfaces',
      'Designed VBA-based reporting frameworks for Finance & Controlling data aggregation',
    ],
    bullets_de: [
      'End-to-End-Automatisierung von Geschaeftsprozessen mit UiPath und Python',
      'VBA-basierte Reporting-Frameworks fuer Finance & Controlling Datenaggregation',
    ]
  }
};

const ALL_PROJECTS = {
  socialengine: {
    period: '2024 –\npresent',
    title_en: 'Social Media Automation Platform – Full-Stack Project',
    title_de: 'Social Media Automation Platform – Full-Stack Projekt',
    bullets_en: [
      'Built production platform: 200+ TypeScript files, 39 API endpoints, PostgreSQL + Redis backend',
      'Implemented AI-driven content scheduling, voice matching, and quality detection',
    ],
    bullets_de: [
      'Produktionsreife Plattform: 200+ TypeScript-Dateien, 39 API-Endpoints, PostgreSQL + Redis',
      'AI-gesteuertes Content Scheduling, Voice Matching und Qualitaetserkennung',
    ]
  },
  aiplatform: {
    period: '2024 –\npresent',
    title_en: 'AI Content Generation Platform',
    title_de: 'AI Content Generation Platform',
    bullets_en: [
      'Image generation pipelines with custom ML models and GPU infrastructure',
      'Automated content pipeline: generation, scheduling, and posting at scale',
    ],
    bullets_de: [
      'Bild-Pipelines mit Custom ML-Modellen und GPU-Infrastruktur',
      'Automatisierte Content Pipeline: Generierung, Scheduling und Posting at Scale',
    ]
  },
  jobpipeline: {
    period: '2025',
    title_en: 'Autonomous Job Application Pipeline',
    title_de: 'Autonomes Bewerbungssystem',
    bullets_en: [
      'Docker-based job scraping with YAML scoring engine (600+ keywords, 15 categories)',
      'Agent orchestration for autonomous applications with browser automation',
    ],
    bullets_de: [
      'Docker-basiertes Job-Scraping mit YAML Scoring Engine (600+ Keywords, 15 Kategorien)',
      'Agent-Orchestrierung fuer autonome Bewerbungen mit Browser-Automation',
    ]
  },
  rag: {
    period: '2024',
    title_en: 'AI Knowledge Systems – University Research',
    title_de: 'AI Knowledge Systems – Uni-Forschungsprojekt',
    bullets_en: [
      'Multi-module RAG system using GPT-4, vector databases, and Streamlit for dynamic knowledge retrieval',
    ],
    bullets_de: [
      'Multi-Modul RAG-System mit GPT-4, Vektordatenbanken und Streamlit fuer dynamische Wissensabfrage',
    ]
  },
  agents: {
    period: '2023 –\npresent',
    title_en: 'LLM Agent Orchestration – Personal R&D',
    title_de: 'LLM Agent Orchestration – Eigenentwicklung',
    bullets_en: [
      'Multi-agent setups using LangChain, CrewAI, MCP, and AutoGen for complex analysis workflows',
      'Pipeline automations orchestrating Claude, GPT-4, Whisper, ElevenLabs, and HuggingFace',
    ],
    bullets_de: [
      'Multi-Agent-Setups mit LangChain, CrewAI, MCP und AutoGen fuer komplexe Analyse-Workflows',
      'Pipeline-Automatisierungen mit Claude, GPT-4, Whisper, ElevenLabs und HuggingFace',
    ]
  }
};

const ALL_EDUCATION = [
  { period: '2022 – 2025',
    title_en: 'University of Berlin – M.Sc. Business Informatics',
    title_de: 'Universitaet Berlin – M.Sc. Wirtschaftsinformatik',
    details_en: ['Focus: Data Analytics, AI/ML, Digital Business | GPA: 1.7'],
    details_de: ['Schwerpunkte: Data Analytics, AI/ML, Digital Business | Note: 1,7'] },
  { period: '2021',
    title_en: 'Partner University, Southeast Asia – Exchange Semester',
    title_de: 'Partneruniversitaet, Suedostasien – Auslandssemester',
    details_en: ['Business Intelligence, E-Commerce'],
    details_de: ['Business Intelligence, E-Commerce'] },
  { period: '2018 – 2022',
    title_en: 'University of Berlin – B.Sc. Business Informatics',
    title_de: 'Universitaet Berlin – B.Sc. Wirtschaftsinformatik',
    details_en: [],
    details_de: [] },
];

const SKILLS_EN = [
  ['AI & LLMs', 'Claude (Opus/Sonnet), GPT-4, Gemini, LangChain, CrewAI, MCP, RAG, Prompt Engineering'],
  ['AI Tools', 'Claude Code, Cursor, Copilot, ComfyUI, Stable Diffusion, LoRA Training'],
  ['Automation', 'n8n, Make.com, UiPath, Power Automate, Docker, Docker Compose'],
  ['Programming', 'Python, TypeScript/Node.js, SQL (PostgreSQL, SQLite), Bash'],
  ['Cloud & DevOps', 'AWS (EC2, S3, Lambda), Azure, Docker, Git, CI/CD, Linux Admin'],
  ['Business Tools', 'Jira, Confluence, Power BI, Tableau, Excel, Salesforce, HubSpot'],
  ['Regulatory', 'MiCAR, DORA, ISO 27001'],
  ['Languages', 'German (native), English (C1), Russian (native)'],
];

const SKILLS_DE = [
  ['AI & LLMs', 'Claude (Opus/Sonnet), GPT-4, Gemini, LangChain, CrewAI, MCP, RAG, Prompt Engineering'],
  ['AI Tools', 'Claude Code, Cursor, Copilot, ComfyUI, Stable Diffusion, LoRA Training'],
  ['Automation', 'n8n, Make.com, UiPath, Power Automate, Docker, Docker Compose'],
  ['Programmierung', 'Python, TypeScript/Node.js, SQL (PostgreSQL, SQLite), Bash'],
  ['Cloud & DevOps', 'AWS (EC2, S3, Lambda), Azure, Docker, Git, CI/CD, Linux Admin'],
  ['Business Tools', 'Jira, Confluence, Power BI, Tableau, Excel, Salesforce, HubSpot'],
  ['Regulatorik', 'MiCAR, DORA, ISO 27001'],
  ['Sprachen', 'Deutsch (Muttersprache), Englisch (C1), Russisch (Muttersprache)'],
];

// ============================================================================
// SECTION BUILDERS
// ============================================================================
function buildExperience() {
  const items = [];
  const label = LANG === 'de' ? 'BERUFSERFAHRUNG' : 'WORK EXPERIENCE';
  items.push(sectionHeading(label));

  const keys = EXP_FILTER.length > 0 ? EXP_FILTER : Object.keys(ALL_EXPERIENCE);
  for (const key of keys) {
    const exp = ALL_EXPERIENCE[key];
    if (!exp) continue;
    const title = LANG === 'de' ? exp.title_de : exp.title_en;
    const bullets_arr = LANG === 'de' ? exp.bullets_de : exp.bullets_en;
    items.push(entryHeader(exp.period, title));
    for (const b of bullets_arr) {
      items.push(bullet(b));
    }
  }
  return items;
}

function buildProjects() {
  const items = [];
  const label = LANG === 'de' ? 'PROJEKTE' : 'PROJECTS';
  items.push(sectionHeading(label));

  const keys = PROJ_FILTER.length > 0 ? PROJ_FILTER : ['socialengine', 'aiplatform', 'jobpipeline', 'agents'];
  for (const key of keys) {
    const proj = ALL_PROJECTS[key];
    if (!proj) continue;
    const title = LANG === 'de' ? proj.title_de : proj.title_en;
    const bullets_arr = LANG === 'de' ? proj.bullets_de : proj.bullets_en;
    items.push(entryHeader(proj.period, title));
    for (const b of bullets_arr) {
      items.push(bullet(b));
    }
  }
  return items;
}

function buildEducation() {
  const items = [];
  const label = LANG === 'de' ? 'BILDUNG' : 'EDUCATION';
  items.push(sectionHeading(label));

  for (const edu of ALL_EDUCATION) {
    const title = LANG === 'de' ? edu.title_de : edu.title_en;
    const details = LANG === 'de' ? edu.details_de : edu.details_en;
    items.push(entryHeader(edu.period, title));
    for (const d of details) {
      items.push(bullet(d));
    }
  }
  return items;
}

function buildSkills() {
  const items = [];
  const label = LANG === 'de' ? 'SKILLS' : 'SKILLS';
  items.push(sectionHeading(label));

  const skills = LANG === 'de' ? SKILLS_DE : SKILLS_EN;
  const rows = skills.map(([lbl, val]) => skillRow(lbl, val));
  items.push(new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [2200, CONTENT_WIDTH - 2200],
    rows: rows
  }));
  return items;
}

// ============================================================================
// BUILD DOCUMENT
// ============================================================================
const content = [];

// --- HEADER ---
const headerCells = [
  new TableCell({
    borders: noBorders, width: { size: CONTENT_WIDTH - 2000, type: WidthType.DXA },
    verticalAlign: VerticalAlign.CENTER,
    children: [
      new Paragraph({ spacing: { after: 30 }, children: [
        new TextRun({ text: PERSONAL.name, bold: true, size: 48, font: FONT })
      ]}),
      new Paragraph({ spacing: { after: 30 }, children: [
        new TextRun({ text: TAGLINE, size: 20, font: FONT, color: "333333" })
      ]}),
      new Paragraph({ spacing: { after: 0 }, children: [
        new TextRun({ text: `${PERSONAL.location}  |  ${PERSONAL.phone}  |  ${PERSONAL.email}`, size: 18, font: FONT, color: "555555" })
      ]}),
    ]
  })
];

if (photoData) {
  headerCells.push(new TableCell({
    borders: noBorders, width: { size: 2000, type: WidthType.DXA },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: AlignmentType.RIGHT,
      children: [new ImageRun({
        type: "png", data: photoData,
        transformation: { width: 90, height: 117 },
        altText: { title: "Photo", description: "Application photo", name: "photo" }
      })]
    })]
  }));
}

content.push(new Table({
  width: { size: CONTENT_WIDTH, type: WidthType.DXA },
  columnWidths: photoData ? [CONTENT_WIDTH - 2000, 2000] : [CONTENT_WIDTH],
  rows: [new TableRow({ children: headerCells })]
}));

// --- SECTIONS IN ORDER ---
const sectionMap = {
  experience: buildExperience,
  skills: buildSkills,
  projects: buildProjects,
  education: buildEducation,
};

for (const section of ORDER) {
  const builder = sectionMap[section];
  if (builder) {
    content.push(...builder());
  } else {
    console.warn(`Unknown section: ${section}`);
  }
}

// --- BUILD ---
const doc = new Document({
  styles: { default: { document: { run: { font: FONT, size: 20 } } } },
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "\u2022",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: DATE_COL + 300, hanging: 300 } } }
      }]
    }]
  },
  sections: [{
    properties: {
      page: {
        size: { width: A4_WIDTH, height: A4_HEIGHT },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN }
      }
    },
    children: content
  }]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync(OUTPUT, buffer);
  console.log(`Created: ${OUTPUT}`);
});
