# Guia completa: Modulo de Cuadrantes de Turnos - La Vila Gran

## Introduccion

El modulo de Cuadrantes permite planificar los turnos de trabajo de todos los empleados de la residencia de forma visual e inteligente. El sistema gestiona automaticamente las restricciones laborales, la cobertura minima de personal y la distribucion equitativa del trabajo.

Los empleados de la residencia se dividen en tres roles:
- **Atenciones**: auxiliares y cuidadores que atienden a los residentes
- **Limpieza**: personal encargado de la limpieza de zonas y habitaciones  
- **Gestion**: personal administrativo y de direccion, que NO aparece en los cuadrantes

---

## Paso 1: Asignar roles a los empleados

Antes de usar los cuadrantes, cada empleado debe tener asignado su rol.

1. Ve al menu superior y entra en **Personas > Empleados**
2. Haz click en **Editar** en un empleado
3. En el formulario lateral, busca el campo **Rol** y selecciona:
   - "Atenciones (auxiliar/cuidador)" para los cuidadores
   - "Limpieza" para el personal de limpieza
   - "Gestion (no aparece en cuadrantes)" para administracion
4. Haz click en **Guardar cambios**
5. Repite para todos los empleados

Los empleados con rol "Gestion" no apareceran en la cuadricula de turnos. Los de "Atenciones" y "Limpieza" si.

---

## Paso 2: Configurar los tipos de turno

La residencia viene con tres turnos predefinidos, pero puedes personalizarlos o crear nuevos.

1. Ve al menu superior y haz click en **Turnos**
2. En la pagina de cuadrantes, haz click en el boton **Tipos de turno**
3. Veras los turnos predefinidos:
   - **M** (Manana): 07:00 a 15:00, color azul
   - **T** (Tarde): 15:00 a 22:00, color naranja
   - **N** (Noche): 22:00 a 07:00, color morado
4. Para modificar un turno, haz click en **Editar** y cambia el horario, color o abreviatura
5. Para crear un turno nuevo (por ejemplo "Refuerzo" de 09:00 a 13:00), haz click en **Anadir tipo de turno**
6. Rellena: nombre, abreviatura (1-5 letras), color, hora inicio, hora fin, minutos de pausa
7. Haz click en **Guardar**

---

## Paso 3: Configurar la cobertura minima

Define cuantos trabajadores necesitas como minimo en cada turno. Esto permite al sistema detectar cuando falta personal y rellenar los huecos automaticamente.

1. En la pagina de cuadrantes, haz click en el boton **Cobertura**
2. Veras una tabla con los turnos y dos columnas: "Entre semana (L-V)" y "Fin de semana (S-D)"
3. Introduce los minimos necesarios. Por ejemplo:
   - Manana: 12 entre semana, 8 fin de semana
   - Tarde: 10 entre semana, 8 fin de semana
   - Noche: 6 entre semana, 6 fin de semana
4. Haz click en **Guardar**

Estos valores se usaran para:
- Mostrar en la cuadricula si la cobertura es suficiente (en verde) o insuficiente (en rojo)
- El generador inteligente rellenara automaticamente los huecos hasta alcanzar estos minimos

---

## Paso 4: Crear patrones de rotacion

Los patrones definen ciclos de turnos que se repiten automaticamente. Esto evita tener que asignar turnos uno a uno.

1. En la pagina de cuadrantes, haz click en el boton **Patrones**
2. Haz click en **Anadir patron**
3. Dale un nombre descriptivo, por ejemplo "Rotacion A - Auxiliares"
4. Indica los dias del ciclo. Por ejemplo, 7 para un ciclo semanal
5. Aparecera una fila de selectores, uno por cada dia del ciclo. Para cada dia, elige el turno:
   - Dia 1: M (Manana)
   - Dia 2: M (Manana)
   - Dia 3: T (Tarde)
   - Dia 4: T (Tarde)
   - Dia 5: N (Noche)
   - Dia 6: L (Libre)
   - Dia 7: L (Libre)
6. Haz click en **Guardar**

Puedes crear varios patrones diferentes. Por ejemplo:
- "Rotacion A": M M T T N L L
- "Rotacion B": T T N N M L L  
- "Rotacion C": N N M M T L L
- "Fijo Manana": M M M M M L L

Asignando diferentes trabajadores a cada rotacion y con fechas de inicio escalonadas, conseguiras que siempre haya personal cubriendo todos los turnos.

---

## Paso 5: Asignar patrones a los trabajadores

Cada trabajador necesita tener asignado un patron (o turno fijo) para que el sistema pueda generar el cuadrante automaticamente.

1. Ve a la pagina de **Turnos** (cuadricula mensual)
2. Haz click en el **nombre de un trabajador** en la primera columna de la cuadricula
3. Se abrira un panel lateral con las opciones de configuracion
4. En "Modo de asignacion", elige:
   - **Patron de rotacion**: para trabajadores que rotan entre turnos
   - **Turno fijo**: para trabajadores que siempre hacen el mismo turno
   - **Sin patron**: para asignar manualmente
5. Si eliges "Patron de rotacion":
   - Selecciona el patron (los que creaste en el paso anterior)
   - Indica la **fecha de inicio del ciclo**. Esta es la fecha desde la cual el sistema empieza a contar el dia 1 del patron
   - **Importante**: para que no todos los trabajadores libren el mismo dia, usa fechas de inicio escalonadas. Por ejemplo, si tienes 3 trabajadores con el mismo patron de 7 dias, asigna fechas de inicio con 2-3 dias de diferencia
6. Si eliges "Turno fijo":
   - Selecciona el turno (M, T o N)
   - Indica la fecha de inicio
7. Haz click en **Guardar configuracion**
8. Repite para todos los trabajadores

Junto al nombre del trabajador en la cuadricula aparecera el nombre del patron asignado en texto pequeno.

---

## Paso 6: Registrar ausencias

Antes de generar el cuadrante, registra las ausencias conocidas (vacaciones, bajas, etc.) para que el sistema no asigne turnos en esos dias.

1. En la pagina de cuadrantes, haz click en el boton **Ausencias**
2. Haz click en **Registrar ausencia**
3. Rellena el formulario:
   - **Trabajador**: selecciona el empleado
   - **Tipo de ausencia**: Vacaciones, Baja medica, Asuntos propios, Permiso retribuido o Festivo
   - **Fecha inicio** y **Fecha fin**: el rango de dias de la ausencia
   - **Notas**: opcional, para aclarar el motivo
4. Haz click en **Guardar**

Las ausencias aparecen en la cuadricula con su color y abreviatura (VAC en verde, BAJ en rojo, etc.) y bloquean la celda para que no se pueda asignar turno en esos dias.

---

## Paso 7: Generar el cuadrante del mes

Ahora que todo esta configurado, puedes generar el cuadrante completo con un solo click.

1. Ve a la pagina de **Turnos** y selecciona el mes con las flechas de navegacion
2. Haz click en el boton azul **Generar mes**
3. Aparecera un dialogo de confirmacion explicando lo que hara el sistema:
   - Aplicar los patrones de rotacion de cada trabajador
   - Saltar los dias con ausencias registradas
   - Eliminar asignaciones que violen la normativa laboral
   - Rellenar los huecos de cobertura automaticamente
   - Preservar los cambios manuales que ya hayas hecho
4. Haz click en **Confirmar**
5. El sistema generara el cuadrante en menos de 1 segundo
6. Aparecera un resumen con los resultados:
   - Cuantos turnos se han generado desde patrones
   - Cuantos dias de ausencia se han saltado
   - Cuantos cambios manuales se han preservado
   - Cuantas asignaciones invalidas se han eliminado
   - Cuantos huecos de cobertura se han rellenado automaticamente
   - La distribucion de turnos: minimo, maximo y media por trabajador
7. La cuadricula se recargara con todos los turnos asignados

---

## Paso 8: Revisar y ajustar

Despues de generar, revisa el cuadrante y haz los ajustes necesarios.

### Revisar la cobertura
- Mira la **fila de cobertura** al final de la cuadricula
- Cada celda muestra el numero de trabajadores por turno. Por ejemplo: "M8/12" significa 8 trabajadores de manana cuando el minimo es 12
- Si el numero esta en **rojo** con borde rojo, significa que no se alcanza el minimo configurado
- Si esta en texto normal, la cobertura es suficiente

### Revisar las horas
- La **columna "Horas"** a la derecha muestra las horas totales planificadas del mes para cada trabajador
- Si esta en rojo, el trabajador tiene demasiadas horas

### Ajustar turnos manualmente
- Haz click en cualquier celda de la cuadricula para cambiar un turno
- Aparecera un selector con las opciones: M, T, N o Libre (X)
- El cambio se guarda automaticamente
- Las celdas con ausencia (VAC, BAJ, etc.) estan bloqueadas y no se pueden modificar

### Copiar una semana
- Si necesitas replicar una semana, usa los botones de **copiar semana** que aparecen encima de la cuadricula
- Esto copia todas las asignaciones de una semana a la siguiente

---

## Paso 9: Validar el cumplimiento laboral

Antes de dar por definitivo el cuadrante, valida que cumple la normativa laboral.

1. Haz click en el boton amarillo **Validar** (icono de escudo)
2. El sistema analizara todo el mes y mostrara alertas si encuentra:
   - **Descanso insuficiente**: menos de 12 horas entre el final de un turno y el inicio del siguiente (requisito legal en Espana)
   - **Exceso de horas**: mas de 40 horas en una semana
   - **Sin dia libre**: una semana completa sin ningun dia de descanso
3. Las alertas aparecen en un panel debajo de la cuadricula con el detalle: trabajador, fecha y problema
4. Las celdas afectadas se marcan con un borde rojo en la cuadricula
5. Si no hay alertas, aparecera un mensaje verde: "Sin alertas - todo correcto"

---

## Paso 10: Exportar el cuadrante

Una vez el cuadrante esta completo y validado, puedes exportarlo.

1. Haz click en **Exportar Excel** para descargar el cuadrante en formato .xlsx
2. El archivo contiene una hoja con todos los trabajadores en filas y los dias del mes en columnas
3. Cada celda muestra la letra del turno (M, T, N o vacio para libre)
4. Puedes imprimir este Excel para colgarlo en el tablon de la residencia

---

## Filtros utiles

La cuadricula tiene varios filtros para facilitar la gestion:

- **Filtro por grupo**: muestra solo los trabajadores asignados a un grupo de residentes concreto
- **Filtro por rol**: muestra solo los de "Atenciones" o solo los de "Limpieza"
- **Navegacion por mes**: flechas para ir al mes anterior o siguiente

---

## Borrar el cuadrante

Si necesitas empezar de cero:

1. Haz click en el boton rojo **Borrar mes**
2. Confirma la accion en el dialogo
3. Se eliminaran TODAS las asignaciones del mes (esta accion no se puede deshacer)
4. Puedes volver a generar el cuadrante desde los patrones

---

## Gestion de una baja imprevista

Cuando un trabajador se da de baja inesperadamente:

1. Ve a **Ausencias** y registra la baja medica con las fechas
2. Vuelve a la cuadricula de **Turnos**
3. Los dias del trabajador afectado apareceran con "BAJ" en rojo y bloqueados
4. La fila de cobertura mostrara en rojo los dias donde ahora falta personal
5. Opcion A: Haz click en la celda de otro trabajador disponible ese dia y asignale el turno manualmente
6. Opcion B: Pulsa **Generar mes** de nuevo - el sistema rellenara los huecos automaticamente con el trabajador mas adecuado, respetando las restricciones laborales y distribuyendo equitativamente

---

## Resumen del flujo de trabajo mensual

1. Asignar roles a empleados nuevos (solo la primera vez)
2. Configurar cobertura minima (solo la primera vez)
3. Crear patrones de rotacion (solo la primera vez)
4. Asignar patrones a trabajadores (solo cuando hay cambios)
5. Registrar ausencias conocidas del mes
6. Pulsar **Generar mes** para crear el cuadrante automaticamente
7. Revisar la cobertura y las horas
8. Ajustar manualmente lo que sea necesario
9. Pulsar **Validar** para confirmar cumplimiento laboral
10. Exportar a Excel para el tablon

---

## Preguntas frecuentes

**Que pasa si genero el mes y ya tenia turnos asignados manualmente?**
Los cambios manuales se preservan. El sistema solo modifica las celdas que no fueron editadas a mano.

**Puedo cambiar un turno despues de generar?**
Si. Haz click en la celda y selecciona otro turno. El cambio se marca como "manual" y se preservara si vuelves a generar.

**Que es la fecha de inicio del ciclo?**
Es la fecha desde la cual el sistema cuenta el dia 1 del patron. Si tu patron es M M T T N L L y la fecha de inicio es el 1 de agosto (viernes), el viernes sera "M", el sabado "M", el domingo "T", etc.

**Por que algunos trabajadores no aparecen en la cuadricula?**
Porque tienen el rol "Gestion" asignado. Los de gestion no forman parte de los cuadrantes de turnos.

**Como se equilibra el trabajo entre los empleados?**
El sistema inteligente puntua a cada trabajador cuando necesita rellenar un hueco. Prioriza a los que tienen menos turnos totales ese mes, menos fines de semana y menos noches, distribuyendo el trabajo de forma equitativa.

**Que significan los colores en la fila de cobertura?**
- Texto normal con formato "M8/12": 8 trabajadores asignados, minimo 12 necesarios
- Borde rojo: cobertura insuficiente (menos trabajadores de los necesarios)
- Sin borde: cobertura correcta
