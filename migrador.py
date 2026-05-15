import os
import psycopg2

# Reemplazá esto por tu URL de conexión real de Supabase / Neon
DATABASE_URL = "postgresql://postgres.exahojhncqzpzjtnzoum:WoodTools2026@aws-1-us-east-2.pooler.supabase.com:6543/postgres"

# Tu catálogo transformado en datos estructurados (Agregué los primeros de ejemplo)
productos_a_cargar = [
    {
        "familia": "Fresas",
        "marca": "WoodTools",
        "nombre_publico": "Fresas Rectas HM",
        "codigo_interno": "FRS0054/1006",
        "aplicacion_material": "Ranuras, cepillado, rebajes en madera",
        "medidas_y_specs": "Diámetro (D): 150mm. Ancho (B): 5 a 100mm. Eje (d): 40mm. Dientes (Z): 4 o 6 rectos sin incisores.",
        "descripcion_tecnica": "Cuerpo cilíndrico rojo. 4 a 6 insertos de HM rectangulares soldados. Deja una ranura rectangular simple y limpia con paredes rectas y fondo plano."
    },
    {
        "familia": "Fresas",
        "marca": "WoodTools",
        "nombre_publico": "Fresas Rectas con Incisores HM",
        "codigo_interno": "FRSI01542/10066",
        "aplicacion_material": "Ranuras en madera (sin astillar)",
        "medidas_y_specs": "Diámetro (D): 150mm. Ancho (B): 15 a 100mm. Eje (d): 40mm. Dientes (Z): 4 o 6 rectos + 2 a 6 incisores.",
        "descripcion_tecnica": "Igual a la recta pero con incisores que garantizan ausencia total de astillado en los bordes superiores de la madera."
    },
    {
        "familia": "Sierras",
        "marca": "Freud",
        "nombre_publico": "Sierra Circular para Melamina (con Incisor)",
        "codigo_interno": "LG3D 0600",
        "aplicacion_material": "Melamina (Uso industrial / Escuadradora)",
        "medidas_y_specs": "Diámetro: 300mm. Espesor: 3,2mm. Eje: 30mm. Material: Carburo de tungsteno (HM) Widia.",
        "descripcion_tecnica": "Corte perfecto en melamina. Requiere máquina con incisor."
    }
]

def migrar_datos():
    print("Conectando a la base de datos...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        for prod in productos_a_cargar:
            cursor.execute("""
                INSERT INTO productos (familia, marca, nombre_publico, codigo_interno, aplicacion_material, medidas_y_specs, descripcion_tecnica)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                prod["familia"], prod["marca"], prod["nombre_publico"], 
                prod["codigo_interno"], prod["aplicacion_material"], 
                prod["medidas_y_specs"], prod["descripcion_tecnica"]
            ))
            print(f"✅ Subido: {prod['nombre_publico']}")
            
        conn.commit()
        cursor.close()
        conn.close()
        print("🎉 ¡Migración completada con éxito!")
        
    except Exception as e:
        print(f"❌ Error al subir a la base de datos: {e}")

if __name__ == "__main__":
    migrar_datos()