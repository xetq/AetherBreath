"""
工具名称: rag_query
功能: 从本地知识库中检索相关文档片段（RAG）
完全本地运行
"""

import os
import logging
import hashlib
import glob
from typing import List, Dict, Optional
from pathlib import Path

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions

# ===== 获取当前文件（rag.py）所在目录，向上两级到项目根 =====
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
HF_CACHE_DIR = PROJECT_ROOT / "hf_cache"

# 强制使用本地模型缓存目录
os.environ['HF_HOME'] = str(HF_CACHE_DIR)
os.environ['TRANSFORMERS_CACHE'] = str(HF_CACHE_DIR)
os.environ['HUGGINGFACE_HUB_CACHE'] = str(HF_CACHE_DIR)

# 强制离线模式（禁止联网）
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_OFFLINE'] = '1'

logger = logging.getLogger(__name__)

# ========== 配置 ==========
KNOWLEDGE_BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent_knowledge_base")
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chroma_db")
COLLECTION_NAME = "agent_knowledge"
DEFAULT_MIN_SCORE = 0.4  # 相关度阈值，低于此值的片段将被过滤

# ========== 1. 文档加载与分块 ==========
def load_documents_from_dir(directory: str) -> List[Dict[str, str]]:
    """
    从目录加载所有 .txt 和 .md 文件
    返回: [{"content": "...", "source": "filename"}, ...]
    """
    documents = []
    if not os.path.exists(directory):
        os.makedirs(directory)
        return documents

    for filename in os.listdir(directory):
        if filename.endswith(('.txt', '.md')):
            filepath = os.path.join(directory, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                documents.append({
                    "content": content,
                    "source": filename
                })
            except Exception as e:
                logger.warning(f"读取文件失败 {filename}: {e}")
    return documents

def chunk_document(content: str, source: str, chunk_size: int = 500, overlap: int = 50) -> List[Dict[str, str]]:
    """
    将文档切分成更小的片段
    """
    chunks = []
    paragraphs = content.split('\n\n')

    current_chunk = ""
    for para in paragraphs:
        if len(current_chunk) + len(para) < chunk_size:
            current_chunk += para + "\n\n"
        else:
            if current_chunk:
                chunks.append({
                    "content": current_chunk.strip(),
                    "source": source
                })
            current_chunk = para + "\n\n"

    if current_chunk:
        chunks.append({
            "content": current_chunk.strip(),
            "source": source
        })

    return chunks

# ========== 2. 向量数据库管理 ==========
class RAGKnowledgeBase:
    def __init__(self, persist_dir: str = CHROMA_PERSIST_DIR):
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )

        # ===== 强制加载本地模型 =====
        model_path = self._get_local_model_path()
        logger.info(f"加载模型: {model_path}")
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=model_path
        )

        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self.embedding_fn
        )

    def _get_local_model_path(self) -> str:
        """
        查找本地模型路径
        返回: 模型文件夹的绝对路径
        """
        # 模型在 hf_cache/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/<hash>/
        model_base = HF_CACHE_DIR / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots"

        if not model_base.exists():
            raise FileNotFoundError(
                f"未找到本地模型。请确认模型已下载到:\n"
                f"  {model_base}\n"
                f"运行以下命令下载模型:\n"
                f"  python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')\""
            )

        # 查找 snapshots 下的子目录
        snapshot_dirs = glob.glob(str(model_base / "*"))

        if not snapshot_dirs:
            raise FileNotFoundError(
                f"模型缓存目录存在，但 snapshots 为空。请重新下载模型:\n"
                f"  python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')\""
            )

        # 取第一个（通常只有一个）snapshot 目录
        return snapshot_dirs[0]

    def build_from_directory(self, directory: str):
        """从目录构建知识库"""
        documents = load_documents_from_dir(directory)
        if not documents:
            logger.warning(f"目录 {directory} 中没有找到文档")
            return

        all_chunks = []
        for doc in documents:
            chunks = chunk_document(doc["content"], doc["source"])
            all_chunks.extend(chunks)

        if not all_chunks:
            logger.warning("没有生成任何分块")
            return

        # 检查是否已有数据，避免重复插入
        existing_count = self.collection.count()
        if existing_count > 0:
            logger.info(f"知识库已有 {existing_count} 个片段，跳过构建")
            return

        # 批量插入
        ids = []
        documents_text = []
        metadatas = []

        for idx, chunk in enumerate(all_chunks):
            doc_id = hashlib.md5(chunk["content"].encode()).hexdigest()
            ids.append(doc_id)
            documents_text.append(chunk["content"])
            metadatas.append({"source": chunk["source"]})

        self.collection.add(
            ids=ids,
            documents=documents_text,
            metadatas=metadatas
        )

        logger.info(f"✅ 知识库构建完成，共 {len(all_chunks)} 个片段")

    def query(self, query: str, top_k: int = 3, min_score: float = DEFAULT_MIN_SCORE) -> str:
        """检索最相关的文档片段"""
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k
            )

            documents = results.get('documents', [[]])[0]
            metadatas = results.get('metadatas', [[]])[0]
            distances = results.get('distances', [[]])[0]

            if not documents:
                return "⚠️ 知识库为空，请先添加文档并运行 --build。"
            # 过滤低相关度片段
            filtered = []
            for doc, meta, dist in zip(documents, metadatas, distances):
                score = 1 - (dist / 2)
                if score >= min_score:
                    filtered.append((doc, meta, score))

            if not filtered:
                return (
                    "🔍 知识库中找到相关内容，但相关度较低（低于阈值 {}），可能不匹配您的问题。\n"
                    "建议尝试使用 `search` 工具进行网络搜索，或补充更相关的文档到知识库。"
                ).format(min_score)

            output = "📚 相关文档片段（引用来源）：\n\n"
            for idx, (doc, meta, score) in enumerate(filtered):
                source = meta.get('source', '未知来源')
                output += f"【来源：{source} | 相关度：{score:.2f}】\n{doc}\n\n"

            return output

        except Exception as e:
            logger.error(f"检索失败: {e}")
            return f"❌ 知识库检索失败: {str(e)}"

# ========== 3. 全局单例 ==========
_rag_instance: Optional[RAGKnowledgeBase] = None

def get_rag() -> RAGKnowledgeBase:
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = RAGKnowledgeBase()
        # 自动构建知识库（如果存在文档目录）
        if os.path.exists(KNOWLEDGE_BASE_DIR):
            _rag_instance.build_from_directory(KNOWLEDGE_BASE_DIR)
        else:
            os.makedirs(KNOWLEDGE_BASE_DIR)
            logger.info(f"创建知识库目录: {KNOWLEDGE_BASE_DIR}")
            logger.info("请将 .txt 或 .md 文档放入该目录，然后重启 Agent")
    return _rag_instance

# ========== 4. 工具函数（供 Agent 调用） ==========
def rag_query(query: str, top_k: int = 3, min_score: float = DEFAULT_MIN_SCORE) -> str:
    """
    RAG 查询工具函数
    参数:
        query: 要查询的问题
        top_k: 返回最相关的片段数量
        min_score: 相关度最低阈值（0-1），低于该值的结果将被过滤
    返回:
        相关文档片段（带来源引用）或提示信息
    """
    try:
        rag = get_rag()
        return rag.query(query, top_k, min_score)
    except Exception as e:
        logger.error(f"RAG 查询失败: {e}")
        return f"❌ 知识库查询失败: {str(e)}"

# ========== 5. 工具 Schema ==========
rag_query_schema = {
    "type": "function",
    "function": {
        "name": "rag_query",
        "description": (
            "查询本地知识库，返回与用户问题最相关的文档片段（含来源和相似度分数）."
            "知识库内容主要为内部资料."
            "注意:"
            "该工具基于向量相似度检索，可能会返回与问题主题相近但范围不同的内容.(例如用户问宽泛概念，知识库只覆盖了其中的子领域)"
            "如果返回的片段相关度较低（分数 < 0.6），或片段内容未能准确回答用户问题，请不要强行使用."
            "无论如何,建议配合其它工具(如search和fetch_url)获取更全面的信息"
            "如果用户问的是：\n"
            "  - 通用学习路线、行业趋势、最新动态\n"
            "  - 知识库中未覆盖的领域（如机器学习、深度学习、通用编程学习这些随时更新的资料）\n"
            "  - 任何需要外部信息支持的问题\n"
            "请优先使用 'search和fetch_url' 工具，而不是 rag_query。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查询的问题，例如 '第一关的核心要点是什么？'"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回最相关的片段数量，默认为 3",
                    "default": 3
                },
                "min_score": {
                    "type": "number",
                    "description": "相关度最低阈值（0-1），低于该值的结果将被过滤。默认 0.4。",
                    "default": 0.4
                }
            },
            "required": ["query"]
        }
    }
}

# ========== 6. 命令行工具：构建知识库 ==========
def build_knowledge_base():
    """独立运行：构建知识库"""
    rag = RAGKnowledgeBase()
    rag.build_from_directory(KNOWLEDGE_BASE_DIR)
    print(f"知识库已构建在: {CHROMA_PERSIST_DIR}")

if __name__ == "__main__":
    build_knowledge_base()