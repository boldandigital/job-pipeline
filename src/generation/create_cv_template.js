/**
 * CV Generator Template
 * FAANG/Cambridge Style with Photo
 *
 * IMPORTANT: Personal data (name, email, phone, location) should be configured
 * via environment variables or a separate config file. The placeholder values
 * below are examples only.
 *
 * Environment variables:
 *   CV_NAME, CV_EMAIL, CV_PHONE, CV_LOCATION, CV_PHOTO_PATH
 *
 * Usage: Modify CONFIG and CONTENT sections, then run: node create_cv_template.js
 * Convert to PDF: soffice --headless --convert-to pdf output.docx
 */

const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, ImageRun,
        AlignmentType, WidthType, BorderStyle, VerticalAlign, LevelFormat, PageBreak } = require('docx');
const fs = require('fs');
const path = require('path');

// ============================================================================
// CONFIG - ADJUST PER POSITION
// ============================================================================

const CONFIG = {
  outputName: 'CV_APPLICANT_COMPANY',  // ADJUST
  language: 'en', // 'de' or 'en'  // ADJUST
  tagline: 'Business Informatics | AI & Digital Innovation', // ADJUST
};

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
// STYLING
// ============================================================================

const noBorder = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };
const sectionLine = { style: BorderStyle.SINGLE, size: 6, color: "000000" };

const photoPath = process.env.CV_PHOTO_PATH || path.join(__dirname, 'photo.png');
const photoData = fs.existsSync(photoPath) ? fs.readFileSync(photoPath) : null;

const FONT = "DejaVu Serif";
const FONT_FALLBACK = "Georgia";
const A4_WIDTH = 11906;
const A4_HEIGHT = 16838;
const MARGIN = 720; // 0.5 inch
const CONTENT_WIDTH = A4_WIDTH - 2 * MARGIN; // 10466

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

function sectionHeading(text) {
  return new Paragraph({
    spacing: { before: 280, after: 80 },
    border: { bottom: sectionLine },
    children: [new TextRun({ text: text.toUpperCase(), bold: true, size: 24, font: FONT })]
  });
}

function experienceHeader(period, titleLine) {
  return new Table({
    width: { size: CONTENT_WIDTH, type: WidthType.DXA },
    columnWidths: [1900, CONTENT_WIDTH - 1900],
    rows: [new TableRow({
      children: [
        new TableCell({
          borders: noBorders, width: { size: 1900, type: WidthType.DXA },
          verticalAlign: VerticalAlign.TOP,
          children: [new Paragraph({ spacing: { after: 40 }, children: [
            new TextRun({ text: period, size: 21, font: FONT })
          ]})]
        }),
        new TableCell({
          borders: noBorders, width: { size: CONTENT_WIDTH - 1900, type: WidthType.DXA },
          verticalAlign: VerticalAlign.TOP,
          children: [new Paragraph({ spacing: { after: 40 }, children: [
            new TextRun({ text: titleLine, bold: true, size: 22, font: FONT })
          ]})]
        })
      ]
    })]
  });
}

function bulletPoint(text, numbRef) {
  return new Paragraph({
    numbering: { reference: numbRef, level: 0 },
    spacing: { after: 40 },
    indent: { left: 1900 + 360, hanging: 360 },
    children: [new TextRun({ text, size: 21, font: FONT })]
  });
}

function skillRow(label, value) {
  return new TableRow({
    children: [
      new TableCell({
        borders: noBorders, width: { size: 2400, type: WidthType.DXA },
        children: [new Paragraph({ spacing: { after: 60 }, children: [
          new TextRun({ text: label, bold: true, size: 21, font: FONT })
        ]})]
      }),
      new TableCell({
        borders: noBorders, width: { size: CONTENT_WIDTH - 2400, type: WidthType.DXA },
        children: [new Paragraph({ spacing: { after: 60 }, children: [
          new TextRun({ text: value, size: 21, font: FONT })
        ]})]
      })
    ]
  });
}

// ============================================================================
// CONTENT - ADJUST PER POSITION (select/rephrase bullets)
// ============================================================================

const CONTENT = {
  education: [
    // ADJUST: reorder and modify details per position
    { period: '2021 – 2026', title: 'University of Berlin – M.Sc. Business Informatics',
      details: ['Current GPA: 1.7 (German scale)', 'Expected graduation: April 2026'] },
    { period: '2023', title: 'Partner University, Southeast Asia – Exchange Semester',
      details: ['International experience in Southeast Asian business environment'] },
    { period: '2017 – 2021', title: 'University of Berlin – B.Sc. Business Informatics',
      details: ['Final grade: 2.5'] },
  ],

  experience: [
    // ADJUST: select and prioritize bullets
    {
      period: '11/2024 –\n09/2025',
      title: 'TechCorp GmbH, Munich – Working Student & Intern, Digital Innovation',
      bullets: [
        'Co-designed regulatory-compliant infrastructure models across Finance, IT, and Compliance departments',
        'Led independent vendor evaluations for integrating blockchain and RegTech solutions into enterprise architecture',
        'Built automated reports and dashboards using Excel (Pivot, Macros) from large-scale datasets',
        'Designed automated workflows for recurring tasks including monitoring and error analysis',
        // ADJUST: add/remove bullets as needed
      ]
    },
    {
      period: '09/2023 –\n02/2024',
      title: 'StartupXYZ, Frankfurt – Intern, Digital Assets & Innovation',
      bullets: [
        'Developed AI-based investment framework using XGBoost for tokenized asset valuation based on multivariate market and blockchain data',
        'Authored cross-technology research papers on digital infrastructure and institutional investment strategies',
        // ADJUST
      ]
    },
    {
      period: '08/2021 –\n12/2022',
      title: 'University Medical Center – Working Student, Clinical IT',
      bullets: [
        'Digitized medical care processes through IT-supported process modeling and data flow analysis',
        'Planned and scaled interdisciplinary training programs supporting sustainable system rollouts',
      ]
    },
    {
      period: '09/2019 –\n07/2021',
      title: 'IndustryCo AG – Working Student, Process Automation',
      bullets: [
        'End-to-end automation of business processes using UiPath and Python, reducing manual interfaces',
        'Designed VBA-based reporting frameworks for Finance & Controlling data aggregation',
      ]
    },
  ],

  projects: [
    // ADJUST: select 2-3 most relevant
    {
      period: '2024',
      title: 'AI Knowledge Systems Development – University Research Project',
      bullets: [
        'Developed multi-module RAG system using GPT-4, vector databases, and Streamlit for dynamic knowledge generation',
        'Designed component-based LLM framework with retrieval logic and semantic vector search',
      ]
    },
    {
      period: '2022 –\npresent',
      title: 'Intelligent Agent Prototyping & LLM Orchestration – Personal Projects',
      bullets: [
        'Built complex multi-agent setups using LangChain, Auto-GPT, and CrewAI for collaborative analysis tasks',
        'Developed pipeline-based automations orchestrating GPT-4, Whisper, ElevenLabs, and HuggingFace',
      ]
    },
  ],

  skills: [
    // ADJUST: move relevant skills to the top
    ['AI & LLMs', 'GPT-4, Claude, OpenAI API, LangChain, CrewAI, RAG Systems, Prompt Engineering'],
    ['Programming', 'Python, JavaScript, SQL, Java, VBA'],
    ['Cloud & Automation', 'Microsoft Azure, AWS Lambda, n8n, Zapier, UiPath, Power Automate'],
    ['Tools', 'Microsoft 365, Power BI, Jira, Confluence, Streamlit'],
    ['Regulatory', 'MiCAR, DORA, ISO 27001'],
    ['Languages', 'German (native), English (fluent), Russian (native)'],
  ],

  interests: 'Weightlifting, Cycling, Gaming',
};

// ============================================================================
// DOCUMENT GENERATION
// ============================================================================

const content = [];

// --- HEADER: Name + Photo ---
const headerCells = [
  new TableCell({
    borders: noBorders, width: { size: CONTENT_WIDTH - 2200, type: WidthType.DXA },
    verticalAlign: VerticalAlign.CENTER,
    children: [
      new Paragraph({ spacing: { after: 40 }, children: [
        new TextRun({ text: PERSONAL.name, bold: true, size: 52, font: FONT })
      ]}),
      new Paragraph({ spacing: { after: 40 }, children: [
        new TextRun({ text: CONFIG.tagline, size: 22, font: FONT })
      ]}),
      new Paragraph({ spacing: { after: 0 }, children: [
        new TextRun({ text: `${PERSONAL.location}  |  ${PERSONAL.phone}  |  ${PERSONAL.email}`, size: 20, font: FONT, color: "444444" })
      ]}),
    ]
  })
];

if (photoData) {
  headerCells.push(new TableCell({
    borders: noBorders, width: { size: 2200, type: WidthType.DXA },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: AlignmentType.RIGHT,
      children: [new ImageRun({
        type: "png", data: photoData,
        transformation: { width: 100, height: 130 },
        altText: { title: "Photo", description: "Application photo", name: "photo" }
      })]
    })]
  }));
}

content.push(new Table({
  width: { size: CONTENT_WIDTH, type: WidthType.DXA },
  columnWidths: photoData ? [CONTENT_WIDTH - 2200, 2200] : [CONTENT_WIDTH],
  rows: [new TableRow({ children: headerCells })]
}));

// --- EDUCATION ---
content.push(sectionHeading(CONFIG.language === 'de' ? 'Bildung' : 'Education'));
for (const edu of CONTENT.education) {
  content.push(experienceHeader(edu.period, edu.title));
  for (const detail of edu.details) {
    content.push(bulletPoint(detail, "bullets"));
  }
}

// --- EXPERIENCE ---
content.push(sectionHeading(CONFIG.language === 'de' ? 'Berufserfahrung' : 'Work Experience'));
for (const exp of CONTENT.experience) {
  content.push(experienceHeader(exp.period, exp.title));
  for (const b of exp.bullets) {
    content.push(bulletPoint(b, "bullets"));
  }
}

// --- PROJECTS ---
content.push(sectionHeading(CONFIG.language === 'de' ? 'Projekte' : 'Projects'));
for (const proj of CONTENT.projects) {
  content.push(experienceHeader(proj.period, proj.title));
  for (const b of proj.bullets) {
    content.push(bulletPoint(b, "bullets"));
  }
}

// --- SKILLS ---
content.push(sectionHeading(CONFIG.language === 'de' ? 'Faehigkeiten & Interessen' : 'Skills & Interests'));
const skillRows = CONTENT.skills.map(([label, value]) => skillRow(label, value));
skillRows.push(skillRow(CONFIG.language === 'de' ? 'Interessen' : 'Interests', CONTENT.interests));
content.push(new Table({
  width: { size: CONTENT_WIDTH, type: WidthType.DXA },
  columnWidths: [2400, CONTENT_WIDTH - 2400],
  rows: skillRows
}));

// --- BUILD DOCUMENT ---
const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } }
  },
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "\u2022",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 2260, hanging: 360 } } }
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
  const filename = `${CONFIG.outputName}.docx`;
  fs.writeFileSync(filename, buffer);
  console.log(`Created: ${filename}`);
});
