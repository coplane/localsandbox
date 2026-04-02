const BM25_K1 = 1.2;
const BM25_B = 0.75;

interface IndexedField {
  tokens: string[];
  termFrequency: Map<string, number>;
  length: number;
}

interface IndexedDocument<TDocument> {
  document: TDocument;
  fields: IndexedField[];
  position: number;
}

interface FieldStats {
  avgLength: number;
  docFrequency: Map<string, number>;
}

export interface SearchHit<TDocument> {
  document: TDocument;
  score: number;
}

export interface SearchOptions {
  minScore?: number;
  minScoreRatio?: number;
}

export interface CorpusField<TDocument> {
  extractText: (document: TDocument) => string;
  weight?: number;
}

export interface IndexOptions<TDocument> {
  fields: CorpusField<TDocument>[];
}

function tokenizeText(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9_]+/g) ?? [];
}

function buildTermFrequency(tokens: string[]): Map<string, number> {
  const termFrequency = new Map<string, number>();
  for (const token of tokens) {
    termFrequency.set(token, (termFrequency.get(token) ?? 0) + 1);
  }
  return termFrequency;
}

function indexField(text: string): IndexedField {
  const tokens = tokenizeText(text);
  return {
    tokens,
    termFrequency: buildTermFrequency(tokens),
    length: tokens.length,
  };
}

function scoreBm25Term(
  termFrequency: number,
  docLength: number,
  avgLength: number,
  docFrequency: number,
  docCount: number,
): number {
  if (termFrequency <= 0 || docFrequency <= 0 || docCount <= 0) {
    return 0;
  }

  const normalizedAvgLength = avgLength > 0 ? avgLength : 1;
  const idf = Math.log(
    1 + (docCount - docFrequency + 0.5) / (docFrequency + 0.5),
  );
  const numerator = termFrequency * (BM25_K1 + 1);
  const denominator = termFrequency +
    BM25_K1 *
      (1 - BM25_B + BM25_B * (docLength / normalizedAvgLength));
  return idf * (numerator / denominator);
}

export class BM25Retriever<TDocument> {
  private readonly documents: IndexedDocument<TDocument>[];
  private readonly fieldStats: FieldStats[];

  constructor(
    private readonly fields: CorpusField<TDocument>[],
    corpus: TDocument[],
  ) {
    this.documents = corpus.map((document, position) => ({
      document,
      fields: fields.map((field) => indexField(field.extractText(document))),
      position,
    }));
    this.fieldStats = fields.map((_, fieldIndex) => {
      const docFrequency = new Map<string, number>();
      let totalLength = 0;

      for (const document of this.documents) {
        const indexedField = document.fields[fieldIndex];
        totalLength += indexedField.length;
        for (const token of new Set(indexedField.tokens)) {
          docFrequency.set(token, (docFrequency.get(token) ?? 0) + 1);
        }
      }

      return {
        avgLength: this.documents.length === 0
          ? 0
          : totalLength / this.documents.length,
        docFrequency,
      };
    });
  }

  search(
    query: string,
    k?: number,
    options?: SearchOptions,
  ): SearchHit<TDocument>[];
  search(
    queries: string[],
    k?: number,
    options?: SearchOptions,
  ): SearchHit<TDocument>[][];
  search(
    queryOrQueries: string | string[],
    k = 10,
    options: SearchOptions = {},
  ): SearchHit<TDocument>[] | SearchHit<TDocument>[][] {
    if (Array.isArray(queryOrQueries)) {
      return queryOrQueries.map((query) => this.searchOne(query, k, options));
    }
    return this.searchOne(queryOrQueries, k, options);
  }

  private searchOne(
    query: string,
    k: number,
    options: SearchOptions,
  ): SearchHit<TDocument>[] {
    const queryTokens = [...new Set(tokenizeText(query))];
    if (queryTokens.length === 0 || k < 1) {
      return [];
    }

    const scored = this.documents.map((document) => {
      let score = 0;

      for (const [fieldIndex, field] of this.fields.entries()) {
        const indexedField = document.fields[fieldIndex];
        const stats = this.fieldStats[fieldIndex];
        const weight = field.weight ?? 1;

        for (const term of queryTokens) {
          score += weight * scoreBm25Term(
            indexedField.termFrequency.get(term) ?? 0,
            indexedField.length,
            stats.avgLength,
            stats.docFrequency.get(term) ?? 0,
            this.documents.length,
          );
        }
      }

      return {
        document: document.document,
        position: document.position,
        score,
      };
    }).filter((entry) => entry.score > 0)
      .sort((left, right) => {
        if (right.score !== left.score) {
          return right.score - left.score;
        }
        return left.position - right.position;
      });

    if (scored.length === 0) {
      return [];
    }

    const minScore = options.minScore ??
      scored[0].score * (options.minScoreRatio ?? 0);

    return scored
      .filter((entry) => entry.score >= minScore)
      .slice(0, k);
  }
}

export function index<TDocument>(
  corpus: TDocument[],
  options: IndexOptions<TDocument>,
): BM25Retriever<TDocument> {
  return new BM25Retriever(options.fields, corpus);
}
