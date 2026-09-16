import ast
import operator
import logging
from typing import Union, Any

logger = logging.getLogger(__name__)

class SafeCalculator:
    """使用 AST 安全计算"""
    
    # 允许的运算符映射
    _operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,      # 一元负号
        ast.Mod: operator.mod,
        ast.FloorDiv: operator.floordiv,
    }

    @classmethod
    def _safe_eval(cls, node: ast.AST) -> Union[int, float]:
        """递归安全评估 AST 节点，只允许数字和基本运算"""
        if isinstance(node, ast.Constant):
            # 只允许数字
            if isinstance(node.value, (int, float)):
                return node.value
            raise TypeError(f"非法常量: {node.value}")
        
        elif isinstance(node, ast.BinOp):
            # 二元运算 (加减乘除)
            left = cls._safe_eval(node.left)
            right = cls._safe_eval(node.right)
            if type(node.op) in cls._operators:
                return cls._operators[type(node.op)](left, right)
            raise TypeError(f"不支持的运算符: {type(node.op).__name__}")
        
        elif isinstance(node, ast.UnaryOp):
            # 一元运算 (负数)
            operand = cls._safe_eval(node.operand)
            if type(node.op) in cls._operators:
                return cls._operators[type(node.op)](operand)
            raise TypeError(f"不支持的一元运算符: {type(node.op).__name__}")
        
        else:
            raise TypeError(f"表达式包含非法结构: {type(node).__name__}")

    @classmethod
    def calculate(cls, expression: str) -> str:
        """生产级入口：解析并计算表达式"""
        try:
            # 1. 清理输入
            expr_clean = expression.strip()
            if not expr_clean:
                return "错误：表达式为空"
            
            # 2. 字符级白名单（只允许数字、运算符、括号、空格、小数点）
            allowed_chars = set("0123456789+-*/().%^ ")
            if any(c not in allowed_chars for c in expr_clean):
                return f"错误：表达式包含非法字符。仅支持 数字 和 + - * / ( ) . % ^"
            
            # 3. 将 ^ 转换为 **（AST 解析时自动处理，但我们需要替换文本）
            # 注意：为了安全，我们不直接替换，而是解析后处理幂运算
            # 但 AST 不识别 ^，所以文本替换必须在解析前：
            safe_expr = expr_clean.replace('^', '**')
            
            # 4. 解析为 AST
            tree = ast.parse(safe_expr, mode='eval')
            
            # 5. 安全计算
            result = cls._safe_eval(tree.body)
            
            # 6. 格式化输出
            if isinstance(result, float):
                # 如果结果是无限大或 NaN
                import math
                if math.isinf(result):
                    return "错误：结果溢出（无穷大）"
                if math.isnan(result):
                    return "错误：结果非数字（NaN）"
                # 如果是整数，去掉 .0
                if result.is_integer():
                    result = int(result)
                else:
                    result = round(result, 6)  # 保留6位小数
            
            return f"✅ 计算结果: {result}"
        
        except SyntaxError as e:
            logger.warning(f"表达式语法错误: {expression}, 错误: {e}")
            return f"错误：表达式语法错误，请检查括号或运算符。"
        except TypeError as e:
            logger.warning(f"表达式类型错误: {expression}, 错误: {e}")
            return f"错误：{str(e)}"
        except ZeroDivisionError:
            return "错误：除以零！"
        except Exception as e:
            logger.error(f"计算异常: {expression}, 异常: {e}", exc_info=True)
            return f"错误：内部计算异常，已记录日志。"

# 暴露给外部的函数接口（保持与老版本兼容）
def calculator(expression: str) -> str:
    return SafeCalculator.calculate(expression)

# 工具 Schema（保持不变，但生产环境下建议用 Pydantic 生成，这里暂用字典）
calculator_schema = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "执行基础数学运算。仅支持 + - * / ( ) . % ^。安全、无副作用。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，例如 '3 + 5 * 2' 或 '10 / 3'"
                }
            },
            "required": ["expression"]
        }
    }
}