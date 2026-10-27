"""Como se hacen las cosas en la aplicacion, para que el asistente lo explique.

El asistente sabia consultar datos pero no sabia explicar procedimientos: a
"como se hace una cura?" contestaba lo que encontraba en la base de datos, que
no es la pregunta. Aqui viven los procedimientos en texto plano.

Por que no se lee de `admin_help.html`, que es donde esta la ayuda de verdad:
raspar HTML en caliente se rompe el dia que alguien retoque la plantilla, y se
rompe en silencio. El precio de tenerlo dos veces es que pueden separarse, asi
que `tests/test_chatbot_guias.py` comprueba que las dos versiones siguen
diciendo lo mismo en lo que importa.
"""
from __future__ import annotations

CURAS = """COMO SE SIGUE UNA CURA

Lo primero, porque de aqui sale todo lo demas: una cura NO es una atencion.
- La atencion dura unos minutos. Se abre acercando el movil a la pulsera y se
  cierra antes de salir de la habitacion.
- La cura dura lo que tarde la herida en curar: dias o semanas. Se abre cuando
  aparece la herida y se cierra cuando ha curado.
Dentro de una atencion se anade el parte de hoy de una cura. La atencion se
cierra; la cura sigue abierta hasta la proxima vez. NUNCA hay que dejar una
atencion abierta para seguir una cura: a las 24 horas se cierra sola, con una
duracion falsa y sin tipo de atencion.

LOS CUATRO ESTADOS
- Activa: esta ahi y no se mueve. Es el estado con el que nace toda cura.
- Curando: va a mejor.
- Empeorando: va a peor. Genera un aviso critico en el panel al momento.
- Curada: cierra la cura. Desaparece del mapa corporal, pero el historial se
  conserva entero.
Si una herida curada se reabre, basta un parte nuevo en otro estado: la fecha
de curacion se borra sola.

EL RECORRIDO, CON UN EJEMPLO
Una ulcera en el sacro, de principio a fin:

Dia 1 - se detecta.
  Durante una atencion, el boton "Registrar herida". Se elige zona (Sacro),
  tipo (Ulcera), tamano (3x2) y gravedad (Moderada), y se describe lo que se
  ve. Al guardar, la cura queda abierta como Activa y se le crea su primer
  parte, con el nombre de quien la vio y la hora. El historial empieza ese dia.

Dia 3 - primer seguimiento.
  Al atender otra vez a esa persona y cerrar la atencion, la cura ya sale en el
  apartado "Curas abiertas", con la zona, el tipo, como estaba y cuando fue el
  ultimo parte. Se toca, se pone como esta hoy (Curando), el tamano, una
  observacion y hasta cuatro fotos con la camara, y se guarda.

Dia 10 - empeora.
  Mismo gesto, pero eligiendo Empeorando. Al guardar, el panel recibe una
  alerta critica al momento. No hace falta avisar por otro canal.

Dia 21 - ha curado.
  Ultimo parte, se elige "Curada - cerrar", foto final y guardar. La cura
  desaparece del mapa corporal y deja de salir en el movil. El historial se
  conserva entero.

EL HISTORIAL
Desde el panel: ficha del residente, pestana Heridas, clic en la herida. Sale
el recorrido completo del ultimo parte al primero. De cada parte queda el
estado, el tamano, lo que se escribio, quien lo escribio, la fecha y la hora,
sus fotos, y si se hizo dentro de una atencion. Todo queda ademas en el
registro de auditoria.

LAS FOTOS
Hasta cuatro por parte. Van en el parte y no en la herida, porque una foto de
una cura solo significa algo con su fecha y su estado al lado. Se reprocesan al
subirlas: se corrige la orientacion del movil y se reescriben como JPEG. No
salen de la aplicacion: no se guardan en la galeria del movil ni se pueden
compartir. Son datos de salud.

LO QUE SUELE SALIR MAL
- Dar de alta otra vez una herida que ya estaba. Hay que mirar "Curas abiertas"
  antes de registrar una nueva.
- No poner el parte porque hoy la herida esta igual. Eso tambien es
  informacion: una herida que lleva dias igual no esta curando, y solo se ve si
  cada visita deja su linea.
- Cerrar la cura antes de tiempo. Curada significa cerrada; ante la duda, poner
  Curando y cerrarla en la visita siguiente.
- Dejar la atencion abierta para seguir la cura."""


CONTROL_DIARIO = """EL INFORME DE CONTROL DIARIO

Esta en Registros / Control diario. Es la hoja del dia, lista para imprimir en
A4 apaisado, con tres bloques:
- Por trabajadora: todo lo que hizo cada una, atenciones y limpiezas juntas,
  con su TOTAL. Va desde que acaba la noche anterior hasta que empieza la de
  ese dia, para que un registro salga una sola vez y los totales de dias
  seguidos se puedan sumar.
- Por residente: el dia natural entero, con su TOTAL y las marcas de lo que le
  ha faltado. Al final, los residentes que no tienen ni un registro.
- Noche: de la tarde de ese dia a la manana siguiente. Las horas se eligen en
  el filtro.

DOS COSAS NO SUMAN EN EL TOTAL, Y SE DICEN
- SIN CERRAR: la sesion sigue abierta y no se sabe cuanto duro.
- REVISAR: dura mas del limite (por defecto 2 horas), casi siempre una sesion
  que nadie cerro.
Debajo de cada TOTAL sale cuanto se ha dejado fuera, para que el numero se
pueda defender.

LAS MARCAS
En Tipos de atencion se puede marcar un tipo como de control diario, con las
veces al dia esperadas y una etiqueta corta. El residente que no la recibio
sale con NO WC; el que la recibio menos veces de las esperadas, con 1 WC. Si
alguien no la ha recibido ni una vez en dos semanas, la marca sale apagada:
probablemente no la necesita."""


GUIAS = {
    'curas': CURAS,
    'control_diario': CONTROL_DIARIO,
}

TEMAS = sorted(GUIAS)
