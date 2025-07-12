import formulas

TYPE_MAP = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool
}

def compute_formula(formula, output_type):
  
    if isinstance(output_type, str):
      output_type = TYPE_MAP.get(output_type.lower())
      if output_type is None:
          raise ValueError(f"Unsupported output_type: {output_type}")
        
    result = formulas.Parser().ast(formula)[1].compile()()
    return output_type(result)