import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";


const EXPECTED_SHA256 = "0ad72f03131b95f322af320960a847c63121dec8a6f0457438f9a5031f9b1432";


function fail(message) {
  throw new Error(message);
}


function replaceRequired(source, search, replacement, expectedCount = 1) {
  const parts = source.split(search);
  const actualCount = parts.length - 1;
  if (actualCount !== expectedCount) {
    fail(`Expected ${expectedCount} occurrence(s) of ${JSON.stringify(search)}, found ${actualCount}`);
  }
  return parts.join(replacement);
}


function replaceSequential(source, search, replacements) {
  let result = source;
  for (const replacement of replacements) {
    const index = result.indexOf(search);
    if (index < 0) {
      fail(`Missing sequential placeholder ${JSON.stringify(search)}`);
    }
    result = result.slice(0, index) + replacement + result.slice(index + search.length);
  }
  if (result.includes(search)) {
    fail(`Unexpected extra placeholder ${JSON.stringify(search)}`);
  }
  return result;
}


function visibleParagraphText(paragraphXml) {
  return paragraphXml
    .replace(/<[^>]+>/g, "")
    .replaceAll("&amp;", "&")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">");
}


function removeParagraphs(documentXml, textFragments) {
  const found = new Map(textFragments.map((fragment) => [fragment, 0]));
  const result = documentXml.replace(/<w:p\b[\s\S]*?<\/w:p>/g, (paragraphXml) => {
    const text = visibleParagraphText(paragraphXml);
    for (const fragment of textFragments) {
      if (text.includes(fragment)) {
        found.set(fragment, found.get(fragment) + 1);
        return "";
      }
    }
    return paragraphXml;
  });
  for (const [fragment, count] of found) {
    if (count !== 1) {
      fail(`Expected one paragraph containing ${JSON.stringify(fragment)}, found ${count}`);
    }
  }
  return result;
}


function replaceParagraphText(documentXml, sourceText, replacementText) {
  let count = 0;
  const result = documentXml.replace(/<w:p\b[\s\S]*?<\/w:p>/g, (paragraphXml) => {
    if (visibleParagraphText(paragraphXml) !== sourceText) {
      return paragraphXml;
    }
    count += 1;
    const openingTag = paragraphXml.match(/^<w:p\b[^>]*>/)?.[0];
    const paragraphProperties = paragraphXml.match(/<w:pPr>[\s\S]*?<\/w:pPr>/)?.[0] || "";
    const runProperties = paragraphXml.match(/<w:rPr>[\s\S]*?<\/w:rPr>/)?.[0] || "";
    if (!openingTag) {
      fail(`Could not preserve paragraph wrapper for ${JSON.stringify(sourceText)}`);
    }
    return `${openingTag}${paragraphProperties}<w:r>${runProperties}<w:t>${replacementText}</w:t></w:r></w:p>`;
  });
  if (count !== 1) {
    fail(`Expected one paragraph equal to ${JSON.stringify(sourceText)}, found ${count}`);
  }
  return result;
}


function moveExplicitPageBreakToHeading(documentXml, headingText) {
  let breakCount = 0;
  let headingCount = 0;
  let result = documentXml.replace(/<w:p\b[\s\S]*?<\/w:p>/g, (paragraphXml) => {
    if (paragraphXml.includes('<w:br w:type="page"/>')) {
      breakCount += 1;
      return "";
    }
    if (visibleParagraphText(paragraphXml) !== headingText) {
      return paragraphXml;
    }
    headingCount += 1;
    if (!paragraphXml.includes("</w:pPr>")) {
      fail(`Heading ${JSON.stringify(headingText)} has no paragraph properties`);
    }
    return paragraphXml.replace("</w:pPr>", "<w:pageBreakBefore/></w:pPr>");
  });
  if (breakCount !== 1 || headingCount !== 1) {
    fail(`Expected one page break and one heading ${JSON.stringify(headingText)}; found ${breakCount}/${headingCount}`);
  }
  return result;
}


function main() {
  const [sourceArgument, outputArgument] = process.argv.slice(2);
  if (!sourceArgument || !outputArgument) {
    fail("Usage: node tools/build_claim_template.mjs SOURCE.docx OUTPUT.docx");
  }

  const sourcePath = path.resolve(sourceArgument);
  const outputPath = path.resolve(outputArgument);
  const sourceBytes = fs.readFileSync(sourcePath);
  const sha256 = crypto.createHash("sha256").update(sourceBytes).digest("hex");
  if (sha256 !== EXPECTED_SHA256) {
    fail(`Reference DOCX checksum mismatch: expected ${EXPECTED_SHA256}, got ${sha256}`);
  }

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "ecopark-claim-template-"));
  try {
    execFileSync("unzip", ["-q", sourcePath, "-d", workDir]);
    const documentPath = path.join(workDir, "word", "document.xml");
    const settingsPath = path.join(workDir, "word", "settings.xml");
    let xml = fs.readFileSync(documentPath, "utf8");

    xml = removeParagraphs(xml, [
      "Приложения",
      "Расчет задолженности по состоянию",
      "Копия или выписка из документа",
      "Копии счетов, уведомлений",
      "[Иные документы при необходимости].",
    ]);
    xml = moveExplicitPageBreakToHeading(xml, "Требования ТСН");

    xml = replaceRequired(xml, "Адрес: [юридический / почтовый адрес ТСН]", "Адрес: {{TSN_ADDRESS}}");
    xml = replaceRequired(
      xml,
      "Телефон: [номер] | Электронная почта: [адрес]",
      "Телефон: {{TSN_PHONE}} | Электронная почта: {{TSN_EMAIL}}",
    );
    xml = replaceRequired(
      xml,
      "Собственнику земельного участка № [номер участка]",
      "Собственнику земельного участка № {{PLOT_NUMBER}}",
    );
    xml = replaceRequired(
      xml,
      "[Фамилия Имя Отчество / наименование собственника]",
      "{{OWNER_NAME}}",
    );
    xml = replaceRequired(
      xml,
      "Адрес участка: [адрес / кадастровый номер]",
      "Адрес участка: {{PLOT_ADDRESS}}",
    );
    xml = replaceRequired(xml, "Исх. № [номер]", "Исх. № {{CLAIM_NUMBER}}");
    xml = replaceRequired(xml, "от [дата] 2026 г.", "от {{CLAIM_DATE}}");
    xml = replaceRequired(xml, "[кадастровый номер]", "{{CADASTRAL_NUMBER}}");
    xml = replaceRequired(xml, "[адрес участка]", "{{PLOT_ADDRESS}}");
    xml = replaceParagraphText(
      xml,
      "Начисления произведены на основании решения общего собрания.",
      "Начисления произведены на основании {{CHARGE_BASIS}}.",
    );
    xml = replaceRequired(
      xml,
      "Сведения для сверки приведены ниже; подробный расчет является приложением к претензии.",
      "По данным бухгалтерского учета ТСН у Вас образовалась задолженность по лицевому счету {{ACCOUNT}} в указанном ниже размере.",
    );
    xml = replaceRequired(
      xml,
      "[дата, по состоянию на которую определен долг]",
      "{{CALCULATION_DATE}}",
    );
    xml = replaceRequired(xml, "[с __.__.____ по __.__.____]", "{{DEBT_PERIOD}}");
    xml = replaceRequired(xml, "[сумма] руб. [коп.] коп.", "{{PRINCIPAL_AMOUNT}}");
    xml = replaceRequired(
      xml,
      "[сумма либо «не начислены»; указать основание]",
      "{{PENALTY_TEXT}}",
    );
    xml = replaceRequired(
      xml,
      "[итоговая сумма] руб. [коп.] коп.",
      "{{TOTAL_AMOUNT}}",
    );
    xml = replaceParagraphText(
      xml,
      "В течение 10 календарных дней со дня получения настоящей претензии (если иной срок не установлен применимым законом или договором) погасить задолженность в размере [итоговая сумма] руб. [коп.] коп.",
      "В течение 10 календарных дней со дня получения настоящей претензии (если иной срок не установлен применимым законом или договором) погасить задолженность в размере {{TOTAL_AMOUNT}}",
    );
    xml = replaceRequired(xml, "[адрес]", "{{TSN_EMAIL}}");
    xml = replaceSequential(xml, "[номер]", [
      "{{PLOT_NUMBER}}",
      "{{CLAIM_NUMBER}}",
      "{{PLOT_NUMBER}}",
    ]);
    xml = replaceRequired(xml, "[дата]", "{{CLAIM_DATE}}");
    xml = replaceRequired(xml, "Претензию и приложения получил(а): ", "Претензию получил(а): ");
    xml = replaceRequired(xml, "[Ф. И. О. получателя]", "{{OWNER_NAME}}");

    const originalPlaceholders = xml.match(/\[[^\]]+\]/g) || [];
    if (originalPlaceholders.length) {
      fail(`Unconverted bracket placeholders: ${originalPlaceholders.join(", ")}`);
    }
    fs.writeFileSync(documentPath, xml, "utf8");

    let settingsXml = fs.readFileSync(settingsPath, "utf8");
    if (!settingsXml.includes("<w:updateFields")) {
      settingsXml = settingsXml.replace(
        "</w:settings>",
        '<w:updateFields w:val="true"/></w:settings>',
      );
    }
    fs.writeFileSync(settingsPath, settingsXml, "utf8");

    fs.rmSync(outputPath, { force: true });
    execFileSync("zip", ["-q", "-X", "-D", "-r", outputPath, "."], { cwd: workDir });
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
  }
}


main();
